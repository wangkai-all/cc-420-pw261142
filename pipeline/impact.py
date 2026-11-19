"""Lineage-aware impact tracing.

Two kinds of impact, deliberately kept apart:

*Row trace (measured)*
    Walks the actual per-row ``origin`` recorded by the transform for this run.
    Start from a violating finding's record and follow parent -> child rows.
    No key-name guessing, no sampling.

*Partition estimate*
    Rules that are inherently partition-scoped (arrival SLA, row-count drift,
    volume anomaly) cannot be attributed to individual records.  They are
    recorded as ``scope='partition'`` and propagated as an *estimate* with
    low confidence and an explicit method; they are never reported as
    "N rows affected".

Material vs traced
------------------
An edge is *material* for a violation when the child consumes the violated
field (``relations.fields`` contains it, or the edge does not enumerate
fields).  Findings from structural rules (``field IS NULL``) are material for
every edge.  Traced-but-immaterial rows are still recorded so a page can say
"lineage touches this report, but not through the broken field".
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field

from . import model
from .rules import new_id, now

MAX_DEPTH = 12


# ------------------------------------------------------------------ graph
@dataclass
class Edge:
    parent: str
    child: str
    child_type: str
    propagation: str
    impact_weight: float
    fields: set[str] | None
    note: str | None


@dataclass
class Graph:
    edges: list[Edge] = field(default_factory=list)
    adj: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    rev: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    datasets: dict[str, dict] = field(default_factory=dict)
    reports: dict[str, dict] = field(default_factory=dict)
    gaps: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load(cls, conn) -> "Graph":
        g = cls()
        g.datasets = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM datasets")}
        g.reports = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM reports")}
        for r in conn.execute("SELECT * FROM relations"):
            fields = set(json.loads(r["fields"])) if r["fields"] else None
            e = Edge(r["parent_id"], r["child_id"], r["child_type"], r["propagation"],
                     float(r["impact_weight"]), fields, r["note"])
            g.edges.append(e)
            g.adj[e.parent].append(e)
            g.rev[e.child].append(e)
        for r in conn.execute("SELECT * FROM lineage_gaps"):
            g.gaps[r["dataset_id"]].append(dict(r))
        return g

    # ---- closure helpers ------------------------------------------------
    def descendants(self, node: str, *, include_reports: bool = True) -> list[tuple[str, int]]:
        seen = {node: 0}
        q = deque([(node, 0)])
        while q:
            cur, d = q.popleft()
            if d >= MAX_DEPTH:
                continue
            for e in self.adj.get(cur, []):
                if not include_reports and e.child_type == "report":
                    continue
                if e.child not in seen or d + 1 < seen[e.child]:
                    seen[e.child] = d + 1
                    q.append((e.child, d + 1))
        seen.pop(node, None)
        return sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))

    def ancestors(self, node: str) -> list[tuple[str, int]]:
        return self.descendants(node) if False else self._walk_rev(node)

    def _walk_rev(self, node: str) -> list[tuple[str, int]]:
        seen = {node: 0}
        q = deque([(node, 0)])
        while q:
            cur, d = q.popleft()
            if d >= MAX_DEPTH:
                continue
            for e in self.rev.get(cur, []):
                if e.parent not in seen or d + 1 < seen[e.parent]:
                    seen[e.parent] = d + 1
                    q.append((e.parent, d + 1))
        seen.pop(node, None)
        return sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))

    def path_edges(self, src: str, dst: str) -> list[Edge] | None:
        """BFS shortest path; returns edges or None."""
        q = deque([(src, [])])
        seen = {src}
        while q:
            cur, path = q.popleft()
            if cur == dst:
                return path
            if len(path) >= MAX_DEPTH:
                continue
            for e in self.adj.get(cur, []):
                if e.child in seen:
                    continue
                seen.add(e.child)
                q.append((e.child, path + [e]))
        return None

    def gaps_near(self, node: str) -> list[dict]:
        out: list[dict] = []
        for ds, _d in [(node, 0)] + self._walk_rev(node):
            for gap in self.gaps.get(ds, []):
                out.append(gap)
        return out


def edge_material(rule_field: str | None, edge: Edge) -> bool:
    if rule_field is None:
        return True            # structural / partition rule: affects the whole row
    if edge.fields is None:
        return True            # consumer does not enumerate its inputs
    return rule_field in edge.fields


# ------------------------------------------------------------------ trace
@dataclass
class RootHit:
    root_dataset: str
    record_id: str
    finding_id: str
    node: str
    node_type: str
    depth: int
    path: list[str]
    material: bool
    weight: float


def _row_origin_index(rows_by_ds: dict[str, list[model.Row]]):
    """(dataset, record_id) -> [(child_dataset, child_row_key)]"""
    idx: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for ds, rows in rows_by_ds.items():
        for r in rows:
            for o in r["origin"]:
                idx[(o["dataset"], o["record_id"])].append((ds, r["row_key"]))
    return idx


def trace_rows(graph: Graph, rows_by_ds: dict[str, list[model.Row]], violations: list[dict]) -> list[RootHit]:
    """BFS each violating record through the measured row graph.

    The root dataset itself is reported at depth 0 (a report that reads that
    dataset directly is affected even though no further row is produced), and
    report nodes are reached through declared export edges.  ``material`` is
    false as soon as an edge on the path does not consume the broken field, so
    a report can be *traced* without being *affected*.
    """
    idx = _row_origin_index(rows_by_ds)
    rel = {(e.parent, e.child): e for e in graph.edges}
    hits: list[RootHit] = []
    for f in violations:
        root_ds = f["dataset_id"]
        root_rec = f["record_id"]
        start = (root_ds, root_rec)
        if start not in idx:
            continue
        best: dict[tuple[str, str], tuple[bool, float, int, list[str]]] = {start: (True, 1.0, 0, [root_ds])}
        q = deque([start])
        while q:
            cur = q.popleft()
            mat, w, depth, path = best[cur]
            if depth >= MAX_DEPTH:
                continue
            for child_ds, child_key in idx.get(cur, []):
                e = rel.get((cur[0], child_ds))
                if e is None:
                    continue  # unregistered edge: never invent impact
                c_mat = mat and edge_material(f["field"], e)
                c_w = w * e.impact_weight
                nxt = (child_ds, child_key)
                prev = best.get(nxt)
                cand = (c_mat, c_w, depth + 1, path + [child_ds])
                if prev is None or (cand[0] and not prev[0]) or (cand[0] == prev[0] and cand[1] > prev[1] + 1e-9):
                    best[nxt] = cand
                    q.append(nxt)

        per_ds: dict[str, tuple[bool, float, int, list[str]]] = {root_ds: (True, 1.0, 0, [root_ds])}
        for (ds, _rec), state in best.items():
            if (ds, _rec) == start:
                continue
            prev = per_ds.get(ds)
            if prev is None or (state[0] and not prev[0]) or (state[0] == prev[0] and state[1] > prev[1] + 1e-9):
                per_ds[ds] = state

        seen_reports: dict[str, tuple[bool, float, int, list[str]]] = {}
        for ds, (mat, w, depth, path) in sorted(per_ds.items()):
            hits.append(RootHit(root_ds, root_rec, f["id"], ds, "dataset", depth, path, mat, round(w, 6)))
            for e in graph.adj.get(ds, []):
                if e.child_type != "report":
                    continue
                r_mat = mat and edge_material(f["field"], e)
                r_w = w * e.impact_weight
                prev = seen_reports.get(e.child)
                cand = (r_mat, r_w, depth + 1, path + [e.child])
                if prev is None or (cand[0] and not prev[0]) or (cand[0] == prev[0] and cand[1] > prev[1] + 1e-9):
                    seen_reports[e.child] = cand
        for rid, (mat, w, depth, path) in seen_reports.items():
            hits.append(RootHit(root_ds, root_rec, f["id"], rid, "report", depth, path, mat, round(w, 6)))
    return hits


def commit_hits(conn, run_id: str, hits: list[RootHit], *, snapshot_date: str) -> int:
    conn.executemany(
        """INSERT INTO lineage_hits(id,run_id,root_dataset,root_record_id,root_finding_id,impacted_node,node_type,
                                    depth,path,material,weight,status,scope,partition_value,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(new_id("lh"), run_id, h.root_dataset, h.record_id, h.finding_id, h.node, h.node_type,
          h.depth, json.dumps(h.path), int(h.material), h.weight, "measured", "row", snapshot_date, None)
         for h in hits],
    )
    by_root: dict[tuple[str, str], list[RootHit]] = defaultdict(list)
    for h in hits:
        if h.node_type == "dataset":
            by_root[(h.root_dataset, h.record_id)].append(h)
    conn.executemany(
        """INSERT OR REPLACE INTO record_impacts(run_id,root_dataset,record_id,root_finding_id,min_depth,
                                                 material_nodes,traced_nodes,computed_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        [(run_id, ds, rec, hs[0].finding_id, min(h.depth for h in hs),
          sum(1 for h in hs if h.material), len(hs), now())
         for (ds, rec), hs in by_root.items()],
    )
    return len(hits)


def partition_estimates(graph: Graph, conn, run_id: str, partitions: list[dict], snapshot_date: str) -> int:
    """Propagate partition-scope violations as estimates (never as measured rows)."""
    n = 0
    for p in partitions:
        rows = conn.execute(
            "SELECT rows, scan_cost_usd FROM partition_costs WHERE dataset_id=? AND partition_value=?",
            (p["dataset_id"], snapshot_date),
        ).fetchone()
        base_rows = int(rows["rows"]) if rows else 0
        for ds, depth in graph.descendants(p["dataset_id"], include_reports=False):
            size = conn.execute(
                "SELECT rows FROM partition_costs WHERE dataset_id=? AND partition_value=?",
                (ds, snapshot_date),
            ).fetchone()
            est_rows = int(size["rows"]) if size else 0
            if not est_rows:
                est_rows = base_rows
            confidence = "low" if depth > 2 else "medium"
            conn.execute(
                """INSERT INTO impact_estimates(id,run_id,dataset_id,partition_key,partition_value,method,
                                                estimated_rows,estimated_cost_usd,confidence,explanation)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (new_id("ie"), run_id, ds, "snapshot_date", snapshot_date, "partition_scan",
                 est_rows, 0.0, confidence,
                 f"partition-scoped violation at {p['dataset_id']} ({p['detail']}); whole partition propagated depth {depth}"),
            )
            n += 1
        conn.execute(
            """INSERT INTO lineage_hits(id,run_id,root_dataset,root_record_id,root_finding_id,impacted_node,node_type,
                                        depth,path,material,weight,status,scope,partition_value,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("lh"), run_id, p["dataset_id"], p["record_id"], p["id"], p["dataset_id"], "dataset",
             0, json.dumps([p["dataset_id"]]), 1, 1.0, "estimated", "partition", snapshot_date, "partition scope"),
        )
        n += 1
    return n
