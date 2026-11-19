"""Report impact + cost materialisation.

Impact and cost answer different questions with different evidence:

Impact  "how much of this report's number is wrong?"
        Row trace over the measured lineage.  The figure is the number of
        distinct root records that reach the report *materially*.  When the
        only evidence is partition-scoped, the report says ``estimated``
        instead of inventing a row count.  When an unregistered consumer exists
        it is listed in ``blind_spots`` and the report is never rendered as
        fully clear.

Cost    "what does recomputing it cost?"
        Always derived from the partition cost book (rows scanned x cost per
        scan), so every figure carries ``cost_kind='estimate'`` and a basis.
        Never presented as a measured value.

A report is ``traced_only`` when lineage reaches it but the violations sit on
fields it does not render — the "same error, different downstream impact" case.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .impact import Graph
from .rules import new_id, now


def _ancestor_datasets(graph: Graph, node: str) -> list[str]:
    if node in graph.datasets:
        return [node] + [d for d, _ in graph.ancestors(node) if d in graph.datasets]
    # report node: walk back to its real parents
    out: list[str] = []
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
    return out


def _report_cost(conn, report: dict, graph: Graph, snapshot_date: str) -> dict[str, Any]:
    nodes = _ancestor_datasets(graph, report["id"])
    rows = 0
    usd = 0.0
    seconds = 0.0
    parts = 0
    for ds in dict.fromkeys(nodes):
        for r in conn.execute(
            "SELECT rows, scan_cost_usd, rows_per_second FROM partition_costs WHERE dataset_id=? AND partition_value=?",
            (ds, snapshot_date),
        ):
            rows += int(r["rows"])
            usd += float(r["scan_cost_usd"])
            seconds += int(r["rows"]) / float(r["rows_per_second"] or 50000)
            parts += 1
    return {
        "cost_rows": rows,
        "cost_usd": round(usd, 4),
        "cost_seconds": round(seconds, 3),
        "basis": f"partition scan over {parts} upstream partitions at snapshot {snapshot_date} (cost book, not measured)",
    }


def materialise(conn, run_id: str, *, snapshot_date: str, kind: str = "actual",
                rule_set_id: str | None = None) -> list[dict[str, Any]]:
    graph = Graph.load(conn)
    if rule_set_id is None:
        rule_set_id = conn.execute("SELECT rule_set_id FROM runs WHERE id=?", (run_id,)).fetchone()["rule_set_id"]
    rules = {r["rule_id"]: dict(r) for r in conn.execute("SELECT * FROM rules WHERE rule_set_id=?", (rule_set_id,))}
    violations = [dict(f) for f in conn.execute("SELECT * FROM findings WHERE run_id=? AND passed=0", (run_id,))]
    row_viol = [f for f in violations if f["scope"] == "row"]
    part_viol = [f for f in violations if f["scope"] == "partition"]

    def critical(f: dict) -> bool:
        r = rules.get(f["rule_id"])
        return bool(r) and r["materiality"] == "critical"

    root_state: dict[tuple[str, str], dict[str, Any]] = {}
    for f in row_viol:
        key = (f["dataset_id"], f["record_id"])
        prev = root_state.get(key)
        st = {"critical": critical(f), "field": f["field"], "dataset": f["dataset_id"], "rule_id": f["rule_id"]}
        if prev is None or (st["critical"] and not prev["critical"]):
            root_state[key] = st
        elif prev["critical"] == st["critical"]:
            prev.setdefault("also", []).append(f["rule_id"])

    hits = [dict(h) for h in conn.execute("SELECT * FROM lineage_hits WHERE run_id=?", (run_id,))]
    material: dict[str, set[tuple[str, str]]] = defaultdict(set)
    traced: dict[str, set[tuple[str, str]]] = defaultdict(set)
    weights: dict[str, float] = defaultdict(float)
    depths: dict[str, int] = defaultdict(lambda: 10**6)
    drivers: dict[str, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    for h in hits:
        if h["status"] != "measured":
            continue
        key = (h["root_dataset"], h["root_record_id"])
        node = h["impacted_node"]
        traced[node].add(key)
        st = root_state.get(key)
        if st is None:
            continue
        depths[node] = min(depths[node], int(h["depth"]))
        if h["material"] and st["critical"]:
            material[node].add(key)
            weights[node] = max(weights[node], float(h["weight"]))
            drivers[node][(st["dataset"], st["rule_id"])] += 1

    # partition-scope evidence: node -> set of datasets with estimate-only evidence
    est_nodes: dict[str, set[str]] = defaultdict(set)
    for p in part_viol:
        est_nodes[p["dataset_id"]].add(p["dataset_id"])
        for ds, _ in graph.descendants(p["dataset_id"], include_reports=True):
            est_nodes[ds].add(p["dataset_id"])

    out: list[dict[str, Any]] = []
    conn.execute("DELETE FROM report_artifacts WHERE run_id=?", (run_id,))
    for rid, rep in graph.reports.items():
        bases = _ancestor_datasets(graph, rid)
        # Report-level hits already apply the field check on the export edge,
        # so prefer them; fall back to upstream dataset hits only if the report
        # has no hits at all (e.g. an unregistered edge).
        if rid in material or rid in traced:
            mat = set(material.get(rid, set()))
            trc = set(traced.get(rid, set()))
        else:
            mat = set()
            trc = set()
            for node in bases:
                mat |= material.get(node, set())
                trc |= traced.get(node, set())
        est: set[str] = set()
        for node in bases:
            est |= est_nodes.get(node, set())
        base_rows_row = conn.execute(
            "SELECT rows FROM partition_costs WHERE dataset_id=? AND partition_value=?", (rep["base_dataset"], snapshot_date)
        ).fetchone()
        base_rows = int(base_rows_row["rows"]) if base_rows_row else 0
        gaps = []
        for ds in bases:
            for g in graph.gaps.get(ds, []):
                if g["direction"] == "downstream":
                    gaps.append({"dataset": ds, "reason": g["reason"], "severity": g["severity"], "note": g["note"]})
        if mat:
            status, confidence = "measured", "high"
        elif est:
            status, confidence = "estimated", "low"
        elif trc:
            status, confidence = "traced_only", "medium"
        elif gaps:
            status, confidence = "unknown", "unknown"
        else:
            status, confidence = "clear", "medium"
        if gaps and status in ("clear", "traced_only"):
            # never claim "clean" while a consumer we cannot see exists
            status = "unknown" if status == "clear" else status
            if status == "unknown":
                confidence = "unknown"
        cost = _report_cost(conn, rep, graph, snapshot_date)
        drv = [{"dataset": ds, "rule_id": rr, "violations": n}
               for (ds, rr), n in sorted(drivers.get(rid, {}).items(), key=lambda kv: -kv[1])[:6]]
        depth = depths.get(rid)
        if depth is None:
            depth = min([depths[n] for n in bases if depths.get(n, 10**6) < 10**6], default=None)
        payload = {
            "report_id": rid,
            "report_name": rep["name"],
            "criticality": rep["criticality"],
            "kind": kind,
            "impact_status": status,
            "material_rows": len(mat),
            "traced_rows": len(trc),
            "affected_pct": round(100.0 * len(mat) / base_rows, 2) if base_rows else 0.0,
            "blind_spots": gaps,
            "path_depth": depth,
            "impact_weight": round(weights.get(rid, 0.0), 6),
            "drivers": drv,
            "cost_kind": "estimate",
            "cost_rows": cost["cost_rows"],
            "cost_usd": cost["cost_usd"],
            "cost_seconds": cost["cost_seconds"],
            "cost_basis": cost["basis"],
            "confidence": confidence,
            "computed_at": now(),
        }
        conn.execute(
            """INSERT OR REPLACE INTO report_artifacts(run_id,report_id,report_name,kind,impact_status,material_rows,
                   traced_rows,affected_pct,blind_spots,path_depth,impact_weight,drivers,cost_kind,cost_rows,cost_usd,
                   cost_seconds,cost_basis,confidence,computed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, rid, rep["name"], kind, status, len(mat), len(trc), payload["affected_pct"],
             json.dumps(gaps, sort_keys=True), depth, payload["impact_weight"], json.dumps(drv, sort_keys=True),
             "estimate", cost["cost_rows"], cost["cost_usd"], cost["cost_seconds"], cost["basis"], confidence,
             payload["computed_at"]),
        )
        out.append(payload)
    return out


def estimate_partition_impact(conn, run_id: str, graph: Graph, partitions: list[dict], snapshot_date: str,
                              rule: dict) -> None:
    for p in partitions:
        for ds in [p["dataset_id"]] + [d for d, _ in graph.descendants(p["dataset_id"], include_reports=False)]:
            row = conn.execute(
                "SELECT rows, scan_cost_usd FROM partition_costs WHERE dataset_id=? AND partition_value=?",
                (ds, snapshot_date),
            ).fetchone()
            est_rows = int(row["rows"]) if row else 0
            cost = float(row["scan_cost_usd"]) if row else 0.0
            conn.execute(
                """INSERT INTO impact_estimates(id,run_id,dataset_id,partition_key,partition_value,method,
                       estimated_rows,estimated_cost_usd,confidence,explanation) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (new_id("ie"), run_id, ds, "snapshot_date", snapshot_date, "partition_scan", est_rows, cost,
                 "low", f"{rule['rule_id']} is partition-scoped ({p['detail']}); estimated_rows is the whole partition, not traced rows"),
            )
