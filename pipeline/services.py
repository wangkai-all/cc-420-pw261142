"""Service API: the rules service, processor service and reports service.

All three are plain functions over a SQLite connection so they can run in one
process (CLI/tests) or behind the HTTP shim in ``pipeline/api.py``.  They share
no mutable Python state: the database is the only contract.  That is what lets
them be deployed and restarted independently.
"""

from __future__ import annotations

import json
from typing import Any

from . import model, rules as rules_svc, runlog
from .impact import Graph
from .processor import Runner
from .rules import DIMENSIONS, new_id, now


# ====================================================================== rules
class RuleService:
    """Owns rule-set versions.  Never mutates a published version."""

    name = "rules"

    def __init__(self, conn):
        self.conn = conn

    def list_rule_sets(self) -> list[dict]:
        return rules_svc.list_rule_sets(self.conn)

    def active(self, name: str | None = None) -> dict | None:
        rs = rules_svc.active_rule_set(self.conn, name)
        if rs:
            self._decorate(rs)
        return rs

    def get(self, rule_set_id: str) -> dict:
        rs = rules_svc.load_rule_set(self.conn, rule_set_id)
        self._decorate(rs)
        return rs

    def _decorate(self, rs: dict) -> None:
        rs["effective_weights"] = {
            r["rule_id"]: round(rules_svc.effective_rule_weight(r), 6) for r in rs["rules"]
        }
        rs["rule_count_by_dimension"] = {
            d: sum(1 for r in rs["rules"] if r["dimension"] == d) for d in DIMENSIONS
        }

    def revise(self, parent_id: str, *, actor: str, weights: dict[str, float] | None = None,
               thresholds: dict[str, float] | None = None,
               rule_patches: dict[str, dict[str, Any]] | None = None,
               added_rules: list[dict[str, Any]] | None = None,
               removed_rules: list[str] | None = None,
               notes: str | None = None) -> dict:
        """Create a new version.  Raises if the parent is superseded."""
        with self.conn:
            child = rules_svc.revise_rule_set(
                self.conn, parent_id, created_by=actor, weights=weights, thresholds=thresholds,
                rule_patches=rule_patches, added_rules=added_rules, removed_rules=removed_rules, notes=notes,
            )
            runlog.event(self.conn, "ruleset.revised", actor=actor, subject=child,
                         payload={"parent": parent_id, "weights": weights, "patches": list((rule_patches or {}).keys()),
                                  "added": [r["rule_id"] for r in (added_rules or [])],
                                  "removed": removed_rules or []})
        return self.get(child)

    def history(self, name: str = "lumen-core") -> list[dict]:
        return rules_svc.list_rule_sets(self.conn, name)

    def snapshot_scores(self, rule_set_id: str) -> list[dict]:
        """Historical scores stay attached to their own rule set."""
        return [dict(r) for r in self.conn.execute(
            "SELECT dataset_id,dimension,score,evaluated,violations,run_id,kind FROM scores WHERE rule_set_id=? ORDER BY dataset_id,dimension",
            (rule_set_id,),
        )]


# ================================================================== processor
class ProcessorService:
    """Runs transforms + rules + tracing.  Exposes queue/execute/control."""

    name = "processor"

    def __init__(self, conn):
        self.conn = conn

    # ---- run creation ---------------------------------------------------
    def create_run(self, *, kind: str = "full", actor: str = "operator",
                   rule_set_id: str | None = None, snapshot_date: str | None = None,
                   scope: dict | None = None, parent_run_id: str | None = None,
                   fault_at_step: str | None = None, mode: str = "actual") -> str:
        rs = rule_set_id or (rules_svc.active_rule_set(self.conn) or {}).get("id")
        if not rs:
            raise ValueError("no active rule set; seed or create one first")
        snap = snapshot_date or self.conn.execute(
            "SELECT MAX(snapshot_date) FROM ingest_snapshots"
        ).fetchone()[0]
        run_id = new_id("run")
        self.conn.execute(
            """INSERT INTO runs(id,kind,mode,status,rule_set_id,snapshot_date,started_at,requested_by,scope,
                   parent_run_id,fault_at_step)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, kind, mode, "queued", rs, snap, now(), actor,
             json.dumps(scope or {}, sort_keys=True), parent_run_id,
             runlog.STEP_NO.get(fault_at_step) if fault_at_step else None),
        )
        runlog.initialise_steps(self.conn, run_id)
        runlog.event(self.conn, "run.created", actor=actor, subject=run_id,
                     payload={"kind": kind, "rule_set": rs, "snapshot": snap, "scope": scope or {}})
        return run_id

    def execute(self, run_id: str, *, interrupt_at: str | None = None, fault_detail: str | None = None) -> dict:
        runner = Runner(self.conn)
        return runner.execute(run_id, interrupt_at=interrupt_at, fault_detail=fault_detail)

    def resume(self, run_id: str, *, actor: str = "operator") -> dict:
        run = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        if run["status"] not in ("interrupted", "failed"):
            raise ValueError(f"run {run_id} is {run['status']}; nothing to resume")
        # drop evidence produced by the partial attempt so resume is not a mix
        # of old and new evaluations for the same rule
        self.conn.execute("DELETE FROM scores WHERE run_id=?", (run_id,))
        self.conn.execute("DELETE FROM lineage_hits WHERE run_id=?", (run_id,))
        self.conn.execute("DELETE FROM record_impacts WHERE run_id=?", (run_id,))
        self.conn.execute("DELETE FROM report_artifacts WHERE run_id=?", (run_id,))
        self.conn.execute("DELETE FROM impact_estimates WHERE run_id=?", (run_id,))
        # findings for steps that completed stay; evaluate/after are redone.  If
        # evaluate completed, keep its findings, otherwise clear them.
        if runlog.step_status(self.conn, run_id, "evaluate") != "done":
            self.conn.execute("DELETE FROM findings WHERE run_id=?", (run_id,))
        if runlog.step_status(self.conn, run_id, "transform") != "done":
            self.conn.execute("DELETE FROM dataset_rows WHERE run_id=?", (run_id,))
        self.conn.execute("UPDATE runs SET status='running', finished_at=NULL WHERE id=?", (run_id,))
        runlog.event(self.conn, "run.resumed", actor=actor, subject=run_id)
        self.conn.commit()
        return self.execute(run_id)

    # ---- targeted recompute --------------------------------------------
    def fix_and_rerun(self, *, source_id: str, record_ids: list[str], patches: list[dict],
                      actor: str = "operator", note: str | None = None) -> dict:
        """Apply an upstream fix, then recompute only what depends on it.

        The affected slice is the offending dataset(s) plus everything
        downstream that the lineage graph says consumes them.  All other
        datasets are carried over from the base run untouched.
        """
        base = self.conn.execute(
            "SELECT * FROM runs WHERE status='completed' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if base is None:
            raise ValueError("no completed run to recompute from")
        graph = Graph.load(self.conn)
        touched_datasets: set[str] = set()
        roots: list[tuple[str, str]] = []
        for rid, patch in zip(record_ids, patches):
            row = self.conn.execute(
                "SELECT payload FROM source_records WHERE source_id=? AND record_id=?", (source_id, rid)
            ).fetchone()
            if row is None:
                raise KeyError(f"{source_id}/{rid}")
            payload = json.loads(row["payload"])
            payload.update(patch)
            self.conn.execute(
                "UPDATE source_records SET payload=?, fixed_at=?, fix_note=? WHERE source_id=? AND record_id=?",
                (json.dumps(payload, sort_keys=True), now(), note, source_id, rid),
            )
            ds = self.conn.execute("SELECT id FROM datasets WHERE source_id=?", (source_id,)).fetchone()
            if ds:
                touched_datasets.add(ds["id"])
                roots.append((ds["id"], rid))
        for ds in list(touched_datasets):
            touched_datasets.update(d for d, _ in graph.descendants(ds, include_reports=False))
        # datasets that are downstream of the changed ones get rebuilt too
        rule_set = base["rule_set_id"]
        run_id = self.create_run(
            kind="targeted", actor=actor, rule_set_id=rule_set, snapshot_date=base["snapshot_date"],
            scope={"datasets": sorted(touched_datasets), "record_keys": [f"{d}/{r}" for d, r in roots],
                   "base_run_id": base["id"], "reason": note or "upstream fix"},
            parent_run_id=base["id"],
        )
        result = self.execute(run_id)
        result["recomputed_datasets"] = sorted(touched_datasets)
        result["base_run_id"] = base["id"]
        return result

    # ---- interruption simulation ---------------------------------------
    def simulate_interrupt(self, *, at_step: str, snapshot_date: str | None = None,
                           actor: str = "operator", detail: str | None = None) -> str:
        if at_step not in runlog.STEP_NO:
            raise ValueError(f"unknown step {at_step!r}")
        run_id = self.create_run(kind="simulation", actor=actor, snapshot_date=snapshot_date,
                                 scope={"reason": "interruption drill"}, fault_at_step=at_step)
        self.execute(run_id, interrupt_at=at_step,
                     fault_detail=detail or f"simulated worker crash while entering {at_step}")
        return run_id

    def steps(self, run_id: str) -> list[dict]:
        return runlog.run_summary(self.conn, run_id)["steps"]

    def runs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT id,kind,mode,status,rule_set_id,snapshot_date,started_at,finished_at,requested_by,parent_run_id,fault_at_step,scope FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )]


# ==================================================================== reports
class ReportService:
    """Read model over completed runs.  Never re-computes scores."""

    name = "reports"

    def __init__(self, conn):
        self.conn = conn

    def latest_run(self, *, include_interrupted: bool = False) -> str | None:
        statuses = ("completed", "interrupted") if include_interrupted else ("completed",)
        q = f"SELECT id FROM runs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY finished_at DESC, started_at DESC LIMIT 1"
        row = self.conn.execute(q, statuses).fetchone()
        return row["id"] if row else None

    def scorecard(self, run_id: str, *, include_estimate: bool = True) -> dict:
        run = dict(self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        kinds = ("actual", "estimate") if include_estimate else ("actual",)
        rows = [dict(r) for r in self.conn.execute(
            f"SELECT * FROM scores WHERE run_id=? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY dataset_id, dimension",
            (run_id, *kinds),
        )]
        thresholds = json.loads(self.conn.execute(
            "SELECT thresholds FROM rule_sets WHERE id=?", (run["rule_set_id"],)
        ).fetchone()["thresholds"])
        by_ds: dict[str, dict] = {}
        for r in rows:
            ds = by_ds.setdefault(r["dataset_id"], {"dataset_id": r["dataset_id"], "dimensions": {}, "kind": r["kind"]})
            if r["dimension"] == "overall":
                ds["overall"] = r["score"]
                ds["evaluated"] = r["evaluated"]
                ds["violations"] = r["violations"]
                ds["severity"] = r["severity"]
            else:
                ds["dimensions"][r["dimension"]] = {
                    "score": r["score"], "evaluated": r["evaluated"], "violations": r["violations"],
                    "threshold": thresholds.get(r["dimension"]),
                    "state": self._state(r["score"], thresholds.get(r["dimension"])),
                    "reused_from": r["reused_from"],
                }
        # coverage: which dimensions a dataset is even measured on
        rule_rows = self.conn.execute(
            "SELECT dataset_id,dimension,COUNT(*) n FROM rules WHERE rule_set_id=? GROUP BY dataset_id,dimension",
            (run["rule_set_id"],),
        )
        coverage: dict[str, dict[str, int]] = {}
        for r in rule_rows:
            coverage.setdefault(r["dataset_id"], {})[r["dimension"]] = r["n"]
        for ds, payload in by_ds.items():
            payload["covered_dimensions"] = sorted(payload["dimensions"].keys())
            payload["uncovered_dimensions"] = [d for d in DIMENSIONS if d not in payload["dimensions"] and coverage.get(ds, {}).get(d)]
        return {
            "run": {k: run[k] for k in ("id", "kind", "status", "rule_set_id", "snapshot_date", "started_at", "finished_at", "mode")},
            "dimensions": DIMENSIONS,
            "datasets": [by_ds[k] for k in sorted(by_ds)],
        }

    @staticmethod
    def _state(score: float, threshold: float | None) -> str:
        if threshold is None:
            return "unmeasured"
        if score < threshold * 0.9:
            return "red"
        if score < threshold:
            return "amber"
        return "green"

    def dataset_detail(self, run_id: str, dataset_id: str) -> dict:
        ds = dict(self.conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone())
        run = dict(self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        rs = rules_svc.load_rule_set(self.conn, run["rule_set_id"])
        rules = [r for r in rs["rules"] if r["dataset_id"] == dataset_id]
        findings = [dict(f) for f in self.conn.execute(
            "SELECT * FROM findings WHERE run_id=? AND dataset_id=? ORDER BY rule_id,record_id", (run_id, dataset_id)
        )]
        by_rule: dict[str, dict] = {}
        for f in findings:
            b = by_rule.setdefault(f["rule_id"], {"rule_id": f["rule_id"], "evaluated": 0, "violations": 0,
                                                  "scope": f["scope"], "reused_from": f["reused_from"], "samples": []})
            b["evaluated"] += 1
            if not f["passed"]:
                b["violations"] += 1
                if len(b["samples"]) < 20:
                    b["samples"].append({
                        "finding_id": f["id"], "record_id": f["record_id"], "field": f["field"],
                        "actual": f["actual"], "expected": f["expected"], "detail": f["detail"],
                        "severity": f["severity"], "scope": f["scope"],
                    })
        rule_meta = {r["rule_id"]: r for r in rules}
        out_rules = []
        for rid, b in by_rule.items():
            meta = rule_meta.get(rid, {})
            b["name"] = meta.get("name")
            b["dimension"] = meta.get("dimension")
            b["materiality"] = meta.get("materiality")
            b["field"] = meta.get("field")
            b["description"] = meta.get("description")
            out_rules.append(b)
        # original facts behind the violations, for drill-through
        facts = []
        for f in findings:
            if f["passed"]:
                continue
            origin = self.conn.execute(
                "SELECT origin FROM dataset_rows WHERE run_id=? AND dataset_id=? AND row_key=?",
                (run_id, dataset_id, f["record_id"]),
            ).fetchone()
            root = None
            if origin:
                meta = json.loads(origin["origin"])
                root = meta.get("root_fact")
                payload = self.conn.execute(
                    "SELECT payload FROM source_records WHERE source_id=? AND record_id=?",
                    (root["source_id"], root["record_id"]),
                ).fetchone() if root and root.get("source_id") else None
                if payload:
                    root = {**root, "payload": json.loads(payload["payload"])}
            downstream = [dict(h) for h in self.conn.execute(
                "SELECT DISTINCT impacted_node,node_type,depth,material,weight,status FROM lineage_hits "
                "WHERE run_id=? AND root_dataset=? AND root_record_id=? ORDER BY depth,impacted_node",
                (run_id, dataset_id, f["record_id"]),
            )]
            facts.append({
                "finding_id": f["id"], "rule_id": f["rule_id"], "record_id": f["record_id"],
                "field": f["field"], "actual": f["actual"], "expected": f["expected"], "detail": f["detail"],
                "root_fact": root, "downstream": downstream,
            })
        thresholds = json.loads(self.conn.execute(
            "SELECT thresholds FROM rule_sets WHERE id=?", (run["rule_set_id"],)
        ).fetchone()["thresholds"])
        scores = []
        for r in self.conn.execute(
            "SELECT dimension,score,evaluated,violations,reused_from,formula,kind,weight,severity "
            "FROM scores WHERE run_id=? AND dataset_id=? ORDER BY dimension",
            (run_id, dataset_id),
        ):
            d = dict(r)
            d["threshold"] = thresholds.get(d["dimension"])
            d["state"] = self._state(d["score"], d["threshold"]) if d["dimension"] != "overall" else None
            scores.append(d)
        return {"dataset": ds, "run_id": run_id, "rules": sorted(out_rules, key=lambda r: r["rule_id"]),
                "violations": facts, "scores": scores}

    def impacts(self, run_id: str) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM report_artifacts WHERE run_id=? ORDER BY report_id", (run_id,)):
            d = dict(r)
            d["blind_spots"] = json.loads(d["blind_spots"] or "[]")
            d["drivers"] = json.loads(d["drivers"] or "[]")
            rep = self.conn.execute("SELECT * FROM reports WHERE id=?", (d["report_id"],)).fetchone()
            d["report"] = dict(rep) if rep else None
            out.append(d)
        return out

    def report_detail(self, run_id: str, report_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM report_artifacts WHERE run_id=? AND report_id=?", (run_id, report_id)).fetchone()
        if row is None:
            raise KeyError(report_id)
        d = dict(row)
        d["blind_spots"] = json.loads(d["blind_spots"] or "[]")
        d["drivers"] = json.loads(d["drivers"] or "[]")
        rep = dict(self.conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone())
        graph = Graph.load(self.conn)
        bases = reports_svc_ancestors(graph, report_id)
        # per upstream dataset: how many material roots reached it, path
        per_dataset = []
        for ds in bases:
            agg = self.conn.execute(
                """SELECT COUNT(DISTINCT root_dataset || ':' || root_record_id) rows,
                          MAX(material) material, MIN(depth) depth
                   FROM lineage_hits WHERE run_id=? AND impacted_node=?""",
                (run_id, ds),
            ).fetchone()
            node = dict(self.conn.execute("SELECT id,name,layer FROM datasets WHERE id=?", (ds,)).fetchone() or {})
            if not node:
                continue
            per_dataset.append({
                "dataset_id": ds, "name": node["name"], "layer": node["layer"],
                "root_records": agg["rows"] or 0,
                "material": bool(agg["material"]),
                "depth": agg["depth"],
                "in_path": True,
            })
        estimates = [dict(r) for r in self.conn.execute(
            "SELECT dataset_id,method,estimated_rows,estimated_cost_usd,confidence,explanation FROM impact_estimates WHERE run_id=? AND dataset_id IN (%s)"
            % ",".join("?" * len(bases)), (run_id, *bases),
        )] if bases else []
        return {"artifact": d, "report": rep, "upstream": sorted(per_dataset, key=lambda x: (x["depth"] if x["depth"] is not None else 99, x["dataset_id"])),
                "estimates": estimates}

    def errors(self, run_id: str, *, dataset_id: str | None = None, rule_id: str | None = None,
               limit: int = 200) -> list[dict]:
        q = "SELECT * FROM findings WHERE run_id=? AND passed=0"
        args: list[Any] = [run_id]
        if dataset_id:
            q += " AND dataset_id=?"
            args.append(dataset_id)
        if rule_id:
            q += " AND rule_id=?"
            args.append(rule_id)
        q += " ORDER BY dataset_id, rule_id, record_id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args)]

    def score_history(self, dataset_id: str, dimension: str = "overall") -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            """SELECT s.run_id,s.rule_set_id,s.score,s.evaluated,s.violations,s.kind,s.computed_at,
                      r.kind AS run_kind, rs.version AS rule_set_version
               FROM scores s JOIN runs r ON r.id=s.run_id JOIN rule_sets rs ON rs.id=s.rule_set_id
               WHERE s.dataset_id=? AND s.dimension=? ORDER BY s.computed_at, s.run_id""",
            (dataset_id, dimension),
        )]

    def lineage(self, dataset_id: str | None = None) -> dict:
        graph = Graph.load(self.conn)
        if dataset_id:
            up = [(d, depth) for d, depth in graph.ancestors(dataset_id)]
            down = [(d, depth) for d, depth in graph.descendants(dataset_id)]
            return {
                "dataset": dataset_id,
                "upstream": [{"id": d, "depth": x, "node": graph.datasets.get(d) or graph.reports.get(d)} for d, x in up],
                "downstream": [{"id": d, "depth": x, "node": graph.datasets.get(d) or graph.reports.get(d)} for d, x in down],
                "gaps": graph.gaps.get(dataset_id, []),
                "unknown": [{"dataset": k, "gaps": v} for k, v in graph.gaps.items() if k == dataset_id],
                "edges": [e.__dict__ for e in graph.edges if e.parent == dataset_id or e.child == dataset_id],
            }
        return {
            "datasets": [{"id": k, "name": v["name"], "layer": v["layer"]} for k, v in graph.datasets.items()],
            "reports": [{"id": k, "name": v["name"], "criticality": v["criticality"], "base_dataset": v["base_dataset"]} for k, v in graph.reports.items()],
            "edges": [{"parent": e.parent, "child": e.child, "child_type": e.child_type,
                       "propagation": e.propagation, "impact_weight": e.impact_weight,
                       "fields": sorted(e.fields) if e.fields else None, "note": e.note} for e in graph.edges],
            "gaps": [dict(g) for g in self.conn.execute("SELECT * FROM lineage_gaps ORDER BY dataset_id")],
        }


# local helper to avoid a name clash with the module-level import
def reports_svc_ancestors(graph: Graph, node: str) -> list[str]:
    out = []
    q = [node]
    seen = set()
    while q:
        cur = q.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for e in graph.rev.get(cur, []):
            out.append(e.parent)
            q.append(e.parent)
    return list(dict.fromkeys(out))
