"""Rule-set versioning.

Rule sets are immutable once written.  Editing weights (or rules) always
produces a *new* row with ``version = parent.version + 1`` and status
``active``; the parent flips to ``superseded``.  Scores carry both
``run_id`` and ``rule_set_id`` so historic scoring is reproducible and never
overwritten.

Dimensions and how they combine
-------------------------------
completeness   expected non-null / valid-shape fields are present
consistency    parent/child relationships agree (referential + row-level)
timeliness     arrival / recency against the source freshness SLA (delay)
validity       type / range / enum checks
uniqueness     duplicate keys within the evaluated scope

Per-dimension score = weighted pass rate, where each rule's weight is its
``weight`` field multiplied by a severity multiplier.  Dataset score = weighted
mean of the dimensions present in the rule set.  Missing rule sets for a
dimension are *not* scored 100: the dimension simply does not participate, and
the API reports ``coverage`` (which dimensions were evaluated).

The old "score" is thus an explainable weighted loss: every point lost maps
back to findings (rule_id, record_id) which map to lineage hits and reports.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .util import canonical

DIMENSIONS = ["completeness", "consistency", "timeliness", "validity", "uniqueness"]
DIMENSION_LABELS = {
    "completeness": "Completeness",
    "consistency": "Consistency",
    "timeliness": "Timeliness",
    "validity": "Validity",
    "uniqueness": "Uniqueness",
}
SEVERITY = {"low": 1.0, "medium": 1.5, "high": 2.5}
DEFAULT_WEIGHTS = {
    "completeness": 0.30,
    "consistency": 0.25,
    "timeliness": 0.15,
    "validity": 0.20,
    "uniqueness": 0.10,
}
DEFAULT_THRESHOLDS = {
    "completeness": 97.0,
    "consistency": 95.0,
    "timeliness": 95.0,
    "validity": 98.0,
    "uniqueness": 99.0,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _validate_weights(weights: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for dim in DIMENSIONS:
        if dim not in weights:
            out[dim] = DEFAULT_WEIGHTS[dim]
            continue
        w = float(weights[dim])
        if w < 0:
            raise ValueError(f"weight for {dim} must be >= 0")
        out[dim] = w
    total = sum(out.values())
    if total <= 0:
        raise ValueError("at least one dimension weight must be > 0")
    # Weights are kept as entered, not renormalised: a dataset's overall score
    # is a weighted mean, so only their ratio matters, and silently changing
    # 0.35 into 0.318 makes the UI disagree with the analyst who typed it.
    return {k: round(v, 6) for k, v in out.items()}


def create_rule_set(
    conn,
    *,
    name: str,
    created_by: str = "system",
    weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
    rules: list[dict[str, Any]] | None = None,
    parent_id: str | None = None,
    notes: str | None = None,
    activate: bool = True,
) -> str:
    """Persist a new immutable rule-set version.  Returns its id."""
    weights = _validate_weights({**DEFAULT_WEIGHTS, **(weights or {})})
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    version = 1
    if parent_id:
        row = conn.execute("SELECT version, name FROM rule_sets WHERE id=?", (parent_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown parent rule set {parent_id}")
        version = int(row["version"]) + 1
        name = name or row["name"]
    rsid = new_id("rs")
    conn.execute(
        """INSERT INTO rule_sets(id,name,status,version,weights,thresholds,created_by,created_at,parent_id,notes)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            rsid,
            name,
            "draft",
            version,
            canonical(weights),
            canonical(thresholds),
            created_by,
            now(),
            parent_id,
            notes,
        ),
    )
    for spec in rules or []:
        add_rule(conn, rsid, spec)
    if activate:
        conn.execute("UPDATE rule_sets SET status='active' WHERE id=?", (rsid,))
        if parent_id:
            conn.execute("UPDATE rule_sets SET status='superseded' WHERE id=? AND status='active'", (parent_id,))
    return rsid


def add_rule(conn, rule_set_id: str, spec: dict[str, Any]) -> None:
    rs = conn.execute("SELECT status FROM rule_sets WHERE id=?", (rule_set_id,)).fetchone()
    if rs is None:
        raise ValueError(f"unknown rule set {rule_set_id}")
    if rs["status"] != "draft":
        raise ValueError("rules can only be added to a draft rule set; create a new version instead")
    if spec["dimension"] not in DIMENSIONS:
        raise ValueError(f"unknown dimension {spec['dimension']}")
    if spec["severity"] not in SEVERITY:
        raise ValueError(f"unknown severity {spec['severity']}")
    conn.execute(
        """INSERT INTO rules(rule_set_id,rule_id,name,dimension,dataset_id,field,severity,expression,weight,materiality,description)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rule_set_id,
            spec["rule_id"],
            spec.get("name", spec["rule_id"]),
            spec["dimension"],
            spec["dataset_id"],
            spec.get("field"),
            spec["severity"],
            canonical(spec["expression"]),
            float(spec.get("weight", 1.0)),
            spec.get("materiality", "critical"),
            spec.get("description"),
        ),
    )


def load_rule_set(conn, rule_set_id: str) -> dict[str, Any]:
    rs = conn.execute("SELECT * FROM rule_sets WHERE id=?", (rule_set_id,)).fetchone()
    if rs is None:
        raise ValueError(f"unknown rule set {rule_set_id}")
    rules = [dict(r) for r in conn.execute("SELECT * FROM rules WHERE rule_set_id=? ORDER BY rule_id", (rule_set_id,))]
    out = dict(rs)
    out["weights"] = json.loads(out["weights"])
    out["thresholds"] = json.loads(out["thresholds"])
    out["rules"] = rules
    for r in out["rules"]:
        r["expression"] = json.loads(r["expression"])
    return out


def active_rule_set(conn, name: str | None = None) -> dict[str, Any] | None:
    if name:
        row = conn.execute("SELECT id FROM rule_sets WHERE status='active' AND name=? ORDER BY version DESC LIMIT 1", (name,)).fetchone()
    else:
        row = conn.execute("SELECT id FROM rule_sets WHERE status='active' ORDER BY created_at DESC, version DESC LIMIT 1").fetchone()
    return load_rule_set(conn, row["id"]) if row else None


def list_rule_sets(conn, name: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT id,name,status,version,created_by,created_at,parent_id,notes,weights FROM rule_sets"
    args: tuple = ()
    if name:
        q += " WHERE name=?"
        args = (name,)
    q += " ORDER BY name, version"
    out = []
    for r in conn.execute(q, args):
        d = dict(r)
        d["weights"] = json.loads(d["weights"])
        d["rule_count"] = conn.execute("SELECT COUNT(*) FROM rules WHERE rule_set_id=?", (d["id"],)).fetchone()[0]
        out.append(d)
    return out


def revise_rule_set(
    conn,
    parent_id: str,
    *,
    created_by: str,
    weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
    rule_patches: dict[str, dict[str, Any]] | None = None,
    added_rules: list[dict[str, Any]] | None = None,
    removed_rules: list[str] | None = None,
    notes: str | None = None,
) -> str:
    """Derive a new version from ``parent_id``.

    Rules are copied; ``rule_patches`` overrides severity/weight/expression for
    an existing rule id; ``added_rules`` appends; ``removed_rules`` drops.
    Because the child is immutable, callers must use the returned id.
    """
    parent = load_rule_set(conn, parent_id)
    if parent["status"] == "superseded":
        raise ValueError("cannot revise a superseded rule set; start from the active version")
    child = create_rule_set(
        conn,
        name=parent["name"],
        created_by=created_by,
        weights=weights or parent["weights"],
        thresholds=thresholds or parent["thresholds"],
        parent_id=parent_id,
        notes=notes,
        activate=False,
    )
    removed = set(removed_rules or [])
    patches = rule_patches or {}
    for rule in parent["rules"]:
        if rule["rule_id"] in removed:
            continue
        spec = {
            "rule_id": rule["rule_id"],
            "name": rule["name"],
            "dimension": rule["dimension"],
            "dataset_id": rule["dataset_id"],
            "field": rule["field"],
            "severity": rule["severity"],
            "expression": rule["expression"],
            "weight": rule["weight"],
            "materiality": rule["materiality"],
            "description": rule["description"],
        }
        spec.update(patches.get(rule["rule_id"], {}))
        add_rule(conn, child, spec)
    for spec in added_rules or []:
        add_rule(conn, child, spec)
    # Activate only once the child is complete, so readers never see zero
    # or half-built active sets.
    conn.execute("UPDATE rule_sets SET status='active' WHERE id=?", (child,))
    conn.execute("UPDATE rule_sets SET status='superseded' WHERE id=? AND status='active'", (parent_id,))
    return child


def effective_rule_weight(rule: dict[str, Any]) -> float:
    """Severity amplifies how much a rule's failure costs the dimension score.

    weight  = importance of the check
    severity = how much a miss matters (low 1.0, medium 1.5, high 2.5)
    The two multiply, so a high-severity rule with 3% failures hurts more than
    a low-severity rule with the same 3%.
    """
    return float(rule["weight"]) * SEVERITY.get(rule["severity"], 1.0)


def score_run(
    conn,
    run_id: str,
    rule_set_id: str,
    *,
    kind: str = "actual",
    computed_at: str | None = None,
    reused_rules: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate findings for ``run_id`` into per-dataset dimension scores.

    Scoring is idempotent for a (run, rule_set, kind) triple; the *run* is the
    unit of history.  A targeted re-run either re-evaluates a rule or records
    ``reused_from`` pointing at the run it copied the finding from.

    Each dataset receives one row per dimension it has rules for, plus an
    ``overall`` row.  ``evaluated`` counts inspected units (records for row
    rules, partitions for partition rules); ``violations`` counts failures.
    """
    rs = load_rule_set(conn, rule_set_id)
    rules = rs["rules"]
    if not rules:
        return []
    computed_at = computed_at or now()
    reused_rules = reused_rules or {}
    findings = [dict(f) for f in conn.execute("SELECT * FROM findings WHERE run_id=? AND rule_set_id=?", (run_id, rule_set_id))]
    by_rule: dict[tuple[str, str], list[dict]] = {}
    for f in findings:
        by_rule.setdefault((f["dataset_id"], f["rule_id"]), []).append(f)
    datasets = sorted({r["dataset_id"] for r in rules})

    conn.execute("DELETE FROM scores WHERE run_id=? AND rule_set_id=? AND kind=?", (run_id, rule_set_id, kind))
    out: list[dict[str, Any]] = []
    for ds in datasets:
        dim_scores: dict[str, dict[str, Any]] = {}
        for dim in DIMENSIONS:
            dim_rules = [r for r in rules if r["dataset_id"] == ds and r["dimension"] == dim]
            if not dim_rules:
                continue
            total_w = 0.0
            pass_w = 0.0
            evaluated = violations = 0
            reused_for_dim: set[str] = set()
            severities: list[str] = []
            for rule in dim_rules:
                rows = by_rule.get((ds, rule["rule_id"]), [])
                if not rows:
                    # never evaluated -> excluded from the score, shown as uncovered
                    continue
                w = effective_rule_weight(rule)
                total_w += w
                evaluated += len(rows)
                p = sum(1 for x in rows if x["passed"])
                violations += len(rows) - p
                # Loss is the raw failure rate amplified by severity; capped at
                # 1 so one catastrophic rule cannot make the score negative.
                loss = min(1.0, (1.0 - p / len(rows)) * SEVERITY.get(rule["severity"], 1.0))
                pass_w += w * (1.0 - loss)
                severities.append(rule["severity"])
                if rule["rule_id"] in reused_rules:
                    reused_for_dim.add(reused_rules[rule["rule_id"]])
            if total_w == 0:
                continue
            dim_scores[dim] = {
                "score": round(100.0 * pass_w / total_w, 4),
                "evaluated": evaluated,
                "violations": violations,
                "weight": rs["weights"][dim],
                "severity": max(severities, key=lambda s: SEVERITY.get(s, 0)),
                "reused_from": sorted(reused_for_dim)[0] if len(reused_for_dim) == 1 else None,
            }

        if not dim_scores:
            continue
        # overall = weighted mean over participating dimensions, renormalised so
        # a dimension with no rules is "not measured", not silently 100.
        wsum = sum(d["weight"] for d in dim_scores.values())
        overall = round(sum(d["score"] * d["weight"] for d in dim_scores.values()) / wsum, 4) if wsum else 0.0
        thresholds = rs["thresholds"]
        breached = {k: round(v["score"] - thresholds.get(k, 0.0), 2) for k, v in dim_scores.items()}
        for dim, payload in dim_scores.items():
            conn.execute(
                """INSERT INTO scores(id,run_id,rule_set_id,dataset_id,dimension,kind,passed,evaluated,violations,
                                       score,weight,severity,computed_at,formula,reused_from)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id("sc"), run_id, rule_set_id, ds, dim, kind, payload["evaluated"] - payload["violations"],
                 payload["evaluated"], payload["violations"], payload["score"], payload["weight"],
                 payload["severity"], computed_at, "weighted_pass_rate", payload["reused_from"]),
            )
            out.append({"dataset_id": ds, "dimension": dim, "threshold": thresholds.get(dim), **payload})
        conn.execute(
            """INSERT INTO scores(id,run_id,rule_set_id,dataset_id,dimension,kind,passed,evaluated,violations,
                                   score,weight,severity,computed_at,formula)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id("sc"), run_id, rule_set_id, ds, "overall", kind,
             sum(d["evaluated"] - d["violations"] for d in dim_scores.values()),
             sum(d["evaluated"] for d in dim_scores.values()),
             sum(d["violations"] for d in dim_scores.values()),
             overall, 1.0,
             max((d["severity"] for d in dim_scores.values()), key=lambda s: SEVERITY.get(s, 0)),
             computed_at, "weighted_dimension_mean"),
        )
        out.append({
            "dataset_id": ds,
            "dimension": "overall",
            "score": overall,
            "evaluated": sum(d["evaluated"] for d in dim_scores.values()),
            "violations": sum(d["violations"] for d in dim_scores.values()),
            "weight": 1.0,
            "dimension_breakdown": {k: d["score"] for k, d in dim_scores.items()},
            "breached": {k: v for k, v in breached.items() if v < 0},
        })
    return out
