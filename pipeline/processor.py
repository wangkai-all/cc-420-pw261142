"""The processor worker: transforms, evaluates rules, traces lineage, scores.

One ``execute_run`` call drives a run through the steps listed in
``runlog.STEPS``.  Each step commits independently, which is what lets a run be
interrupted mid-flight and resumed:
  * a simulated fault is raised *inside* a step, after the previous ones committed
  * resume starts from the first step that is not ``done``/``reused``
  * targeted re-runs recompute only the datasets downstream of the records that
    were fixed, and copy (not re-evaluate) findings for unaffected rules.

The service never overwrites history: it writes new runs, new findings, new
scores.  ``reused_from`` makes it visible when a number came from an earlier
run rather than fresh computation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from . import checks, model, reports as reports_svc, rules as rules_svc, runlog
from .impact import Graph, commit_hits, trace_rows
from .rules import new_id, now

FAULT_STEPS = {"prepare", "ingest", "transform", "evaluate", "trace", "score", "reports", "finalize"}


class Runner:
    def __init__(self, conn, *, actor: str = "worker"):
        self.conn = conn
        self.actor = actor

    # -------------------------------------------------------------- helpers
    def _snapshots(self, snapshot_date: str) -> dict[tuple[str, str], dict]:
        return {
            (r["source_id"], r["snapshot_date"]): dict(r)
            for r in self.conn.execute("SELECT * FROM ingest_snapshots WHERE snapshot_date=?", (snapshot_date,))
        }

    def _ctx(self, snapshot_date: str, rows: dict[str, list[model.Row]], ds: str) -> dict[str, Any]:
        snaps = self._snapshots(snapshot_date)
        sla = {r["id"]: int(r["freshness_sla_min"]) for r in self.conn.execute("SELECT id,freshness_sla_min FROM sources")}
        src = {r["id"]: r["source_id"] for r in self.conn.execute("SELECT id,source_id FROM datasets")}
        # staging/mart datasets have no source of their own: walk up the
        # declared lineage until a source is found, so a timeliness rule on
        # stg_payment reports the payment feed's arrival, not "unknown".
        def resolve_source(ds: str, seen: set[str] | None = None) -> str | None:
            seen = seen or set()
            if ds in seen:
                return None
            seen.add(ds)
            if src.get(ds):
                return src[ds]
            row = self.conn.execute("SELECT parent_id FROM relations WHERE child_id=? AND child_type='dataset'", (ds,)).fetchone()
            return resolve_source(row["parent_id"], seen) if row else None

        source_of = {ds: resolve_source(ds) for ds in src}
        return {
            "now": datetime.fromisoformat(snapshot_date + "T06:00:00+00:00"),
            "datasets": rows,
            "snapshots": snaps,
            "sla_minutes": sla,
            "dataset_source": source_of,
            "snapshot_date": snapshot_date,
        }

    # -------------------------------------------------------------- steps
    def step_prepare(self, run: dict) -> str:
        return f"rule set {run['rule_set_id']}"

    def step_ingest(self, run: dict) -> str:
        snaps = self._snapshots(run["snapshot_date"])
        late = [f"{s}/{d}" for (s, d), v in snaps.items() if v["late"]]
        return f"{len(snaps)} source snapshots, {len(late)} late" + (f": {', '.join(sorted(late))}" if late else "")

    def step_transform(self, run: dict) -> str:
        scope = json.loads(run["scope"]) if run["scope"] else {}
        only = set(scope.get("datasets") or [])
        base: dict[str, list[model.Row]] = {}
        if scope.get("base_run_id"):
            base = model.load_rows(self.conn, scope["base_run_id"])
        if only:
            # targeted rebuild: also keep rows of unaffected datasets from base
            built = model.build_all(self.conn, run["snapshot_date"], only=only, base=base)
        else:
            built = model.build_all(self.conn, run["snapshot_date"])
        n = model.persist(self.conn, run["id"], run["snapshot_date"], built)
        return f"{n} rows across {len([d for d in built if d in model.BUILDERS])} datasets" + (f" (targeted: {sorted(only)})" if only else "")

    def step_evaluate(self, run: dict) -> str:
        rs = rules_svc.load_rule_set(self.conn, run["rule_set_id"])
        snap = run["snapshot_date"]
        rows = model.load_rows(self.conn, run["id"])
        scope = json.loads(run["scope"]) if run["scope"] else {}
        only_rules = set(scope.get("rules") or [])
        base_run = scope.get("base_run_id")
        # rules whose inputs were not recomputed are copied from the base run
        rebuilt = set(scope.get("datasets") or [])
        output_datasets = set(rows.keys())
        reused: dict[str, str] = {}
        findings: list[dict] = []
        for rule in rs["rules"]:
            if only_rules and rule["rule_id"] not in only_rules:
                continue
            dataset = rule["dataset_id"]
            trusted = (not rebuilt) or dataset in rebuilt or dataset not in output_datasets
            if base_run and not trusted:
                copied = self.conn.execute(
                    "SELECT * FROM findings WHERE run_id=? AND rule_id=?", (base_run, rule["rule_id"])
                ).fetchall()
                if copied:
                    for c in copied:
                        findings.append({
                            "rule": rule,
                            "record_id": c["record_id"],
                            "passed": c["passed"],
                            "actual": None if c["actual"] is None else json.loads(c["actual"]),
                            "expected": None if c["expected"] is None else json.loads(c["expected"]),
                            "detail": c["detail"],
                            "scope": c["scope"],
                            "partition_value": c["partition_value"],
                            "reused_from": base_run,
                        })
                    reused[rule["rule_id"]] = base_run
                    continue
            ctx = self._ctx(snap, rows, dataset)
            ds_rows = rows.get(dataset, [])
            try:
                results = checks.evaluate(rule, ds_rows, ctx)
            except Exception as exc:  # noqa: BLE001 - record and continue
                findings.append({
                    "rule": rule, "record_id": f"{dataset}:__error__", "passed": False,
                    "actual": None, "expected": "rule evaluates", "detail": f"evaluator error: {exc}",
                    "scope": "row", "partition_value": None, "reused_from": None,
                })
                continue
            for r in results:
                findings.append({"rule": rule, **r, "reused_from": None})
        self.conn.executemany(
            """INSERT INTO findings(id,run_id,rule_set_id,rule_id,dataset_id,record_id,dimension,field,severity,
                                    passed,actual,expected,detail,scope,partition_value,snapshot_date,reused_from)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(
                new_id("fd"), run["id"], run["rule_set_id"], f["rule"]["rule_id"], f["rule"]["dataset_id"],
                f["record_id"], f["rule"]["dimension"], f["rule"].get("field"), f["rule"]["severity"],
                int(bool(f["passed"])),
                None if f.get("actual") is None else json.dumps(f["actual"], default=str),
                None if f.get("expected") is None else json.dumps(f["expected"], default=str),
                f.get("detail"), f.get("scope", "row"), f.get("partition_value"), snap, f.get("reused_from"),
            ) for f in findings],
        )
        self._reused_rules = reused
        passed = sum(1 for f in findings if f["passed"])
        estimated = sum(1 for f in findings if f.get("scope") == "partition")
        return (f"{len(findings)} findings, {len(findings) - passed} violations, "
                f"{len(reused)} rules reused from earlier runs, {estimated} partition-scoped")

    def step_trace(self, run: dict) -> str:
        rs = rules_svc.load_rule_set(self.conn, run["rule_set_id"])
        by_rule = {r["rule_id"]: r for r in rs["rules"]}
        rows = model.load_rows(self.conn, run["id"])
        violations = [
            dict(f) for f in self.conn.execute(
                "SELECT * FROM findings WHERE run_id=? AND passed=0 AND scope='row'", (run["id"],)
            )
        ]
        for v in violations:
            rule = by_rule.get(v["rule_id"])
            if rule is not None:
                v["field"] = v["field"] if v["field"] is not None else rule.get("field")
        graph = Graph.load(self.conn)
        hits = trace_rows(graph, rows, violations)
        commit_hits(self.conn, run["id"], hits, snapshot_date=run["snapshot_date"])
        # partition-scope evidence recorded separately as estimates
        parts = [dict(f) for f in self.conn.execute(
            "SELECT * FROM findings WHERE run_id=? AND passed=0 AND scope='partition'", (run["id"],)
        )]
        for p in parts:
            rule = by_rule.get(p["rule_id"])
            if rule:
                reports_svc.estimate_partition_impact(self.conn, run["id"], graph, [p], run["snapshot_date"], rule)
        material = sum(1 for h in hits if h.material)
        return f"{len(hits)} lineage hits ({material} material) from {len(violations)} violating records, {len(parts)} partition estimates"

    def step_score(self, run: dict) -> str:
        reused = getattr(self, "_reused_rules", {}) or {}
        if not reused:
            reused = {
                r["rule_id"]: r["reused_from"]
                for r in self.conn.execute(
                    "SELECT DISTINCT rule_id,reused_from FROM findings WHERE run_id=? AND reused_from IS NOT NULL", (run["id"],)
                )
            }
        scores = rules_svc.score_run(self.conn, run["id"], run["rule_set_id"], reused_rules=reused)
        return f"{len(scores)} score rows"

    def step_reports(self, run: dict) -> str:
        arts = reports_svc.materialise(self.conn, run["id"], snapshot_date=run["snapshot_date"])
        measured = sum(1 for a in arts if a["impact_status"] == "measured")
        est = sum(1 for a in arts if a["impact_status"] == "estimated")
        unknown = sum(1 for a in arts if a["impact_status"] == "unknown")
        traced = sum(1 for a in arts if a["impact_status"] == "traced_only")
        return (f"{len(arts)} reports: {measured} measured, {est} estimated, "
                f"{traced} traced only, {unknown} unknown lineage")

    def step_finalize(self, run: dict) -> str:
        return "run closed"

    # -------------------------------------------------------------- driver
    def execute(self, run_id: str, *, interrupt_at: str | None = None, fault_detail: str | None = None) -> dict[str, Any]:
        run = dict(self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        steps: list[tuple[str, Callable[[dict], str]]] = [
            ("prepare", self.step_prepare),
            ("ingest", self.step_ingest),
            ("transform", self.step_transform),
            ("evaluate", self.step_evaluate),
            ("trace", self.step_trace),
            ("score", self.step_score),
            ("reports", self.step_reports),
            ("finalize", self.step_finalize),
        ]
        done = runlog.completed_steps(self.conn, run_id)
        self.conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        self.conn.commit()
        interrupted = False
        message = ""
        for name, fn in steps:
            if name in done:
                runlog.mark(self.conn, run_id, name, "reused" if name != "finalize" else "done", "resumed: already complete")
                self.conn.commit()
                continue
            if interrupt_at == name:
                runlog.mark(self.conn, run_id, name, "interrupted", fault_detail or f"simulated interrupt before {name}")
                runlog.event(self.conn, "run.interrupted", actor=self.actor, subject=run_id,
                             payload={"step": name, "detail": fault_detail})
                self.conn.execute(
                    "UPDATE runs SET status='interrupted', finished_at=?, fault_at_step=? WHERE id=?",
                    (now(), runlog.STEP_NO[name], run_id),
                )
                self.conn.commit()
                interrupted = True
                break
            runlog.mark(self.conn, run_id, name, "running")
            self.conn.commit()
            try:
                detail = fn(run)
            except Exception as exc:  # noqa: BLE001
                runlog.mark(self.conn, run_id, name, "failed", f"{type(exc).__name__}: {exc}")
                self.conn.execute("UPDATE runs SET status='failed', finished_at=? WHERE id=?", (now(), run_id))
                self.conn.commit()
                raise
            runlog.mark(self.conn, run_id, name, "done", detail)
            self.conn.commit()
            message += ("" if not message else "; ") + f"{name}: {detail}"
        if not interrupted:
            # partial if any step never produced evidence
            self.conn.execute("UPDATE runs SET status='completed', finished_at=? WHERE id=?", (now(), run_id))
            runlog.event(self.conn, "run.completed", actor=self.actor, subject=run_id)
            self.conn.commit()
        return {"run_id": run_id, "status": "interrupted" if interrupted else "completed", "detail": message}
