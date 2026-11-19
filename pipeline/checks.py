"""Rule evaluation.

A rule ``expression`` is a JSON spec.  Supported ops:

  {"op": "not_null",        "field": "customer_id"}
  {"op": "regex",           "field": "email", "pattern": "..."}
  {"op": "range",           "field": "total", "min": 0, "max": 100000}
  {"op": "enum",            "field": "status", "values": ["paid", ...]}
  {"op": "unique",          "field": "order_id"}
  {"op": "unique_compound", "fields": ["order_id", "line_no"]}
  {"op": "not_stale",       "field": "updated_at", "hours": 24}
  {"op": "arrival_sla",     "hours": 0}                # dataset timeliness
  {"op": "foreign_key",     "field": "customer_id", "ref_dataset": "raw_customer"}
  {"op": "sum_match",       "right": "raw_order", "right_field": "total",
                            "left_field": "line_total", "group_by": "order_id", "tolerance": 0.01}
  {"op": "equals_field",    "field": "currency", "other": "order_currency"}
  {"op": "row_count_drift", "expected": 24, "tolerance_pct": 5}   # partition scope
  {"op": "volume_anomaly",  "min_rows": 200}                       # partition scope

Row-scoped evaluators emit one finding per inspected record.  Partition-scoped
evaluators emit one finding with ``scope='partition'`` and ``partition_value``;
the impact layer then reports those as *estimated*, never as measured rows.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from .util import as_float

Evaluator = Callable[[dict, list[dict], dict], list[dict]]

PARTITION_OPS = {"arrival_sla", "row_count_drift", "volume_anomaly"}


def _f(rule: dict, record_id: str, passed: bool, actual: Any, expected: Any, detail: str, *,
       scope: str = "row", partition_value: str | None = None) -> dict:
    return {
        "record_id": record_id,
        "passed": bool(passed),
        "actual": None if actual is None else str(actual),
        "expected": None if expected is None else str(expected),
        "detail": detail,
        "field": rule.get("field"),
        "scope": scope,
        "partition_value": partition_value,
    }


def _parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------------ evaluators
def ev_not_null(rule, rows, ctx):
    f = rule["expression"]["field"]
    return [_f(rule, r["row_key"], r["payload"].get(f) not in (None, ""), r["payload"].get(f), "non-empty", f"{f} is null/empty") for r in rows]


def ev_regex(rule, rows, ctx):
    ex = rule["expression"]
    rx = re.compile(ex["pattern"])
    out = []
    for r in rows:
        v = r["payload"].get(ex["field"])
        ok = isinstance(v, str) and rx.match(v) is not None
        out.append(_f(rule, r["row_key"], ok, v, f"regex {ex['pattern']}", f"{ex['field']} format invalid"))
    return out


def ev_range(rule, rows, ctx):
    ex = rule["expression"]
    lo, hi = ex.get("min"), ex.get("max")
    out = []
    for r in rows:
        raw = r["payload"].get(ex["field"])
        v = as_float(raw)
        ok = v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)
        out.append(_f(rule, r["row_key"], ok, raw, f"[{lo}, {hi}]", f"{ex['field']} out of range"))
    return out


def ev_enum(rule, rows, ctx):
    ex = rule["expression"]
    allowed = sorted(ex["values"])
    return [_f(rule, r["row_key"], r["payload"].get(ex["field"]) in allowed, r["payload"].get(ex["field"]), allowed, f"{ex['field']} not an allowed value") for r in rows]


def ev_unique(rule, rows, ctx):
    f = rule["expression"]["field"]
    counts: dict[Any, int] = {}
    for r in rows:
        counts[r["payload"].get(f)] = counts.get(r["payload"].get(f), 0) + 1
    return [_f(rule, r["row_key"], counts.get(r["payload"].get(f), 0) == 1, r["payload"].get(f), "single occurrence", f"duplicate {f}") for r in rows]


def ev_unique_compound(rule, rows, ctx):
    fs = rule["expression"]["fields"]
    counts: dict[tuple, int] = {}
    for r in rows:
        t = tuple(r["payload"].get(f) for f in fs)
        counts[t] = counts.get(t, 0) + 1
    out = []
    for r in rows:
        t = tuple(r["payload"].get(f) for f in fs)
        out.append(_f(rule, r["row_key"], counts[t] == 1, "|".join(map(str, t)), "unique combination", f"duplicate ({','.join(fs)})"))
    return out


def ev_not_stale(rule, rows, ctx):
    ex = rule["expression"]
    now = ctx["now"]
    hours = float(ex["hours"])
    out = []
    for r in rows:
        dt = _parse_ts(r["payload"].get(ex["field"]))
        lag = None if dt is None else (now - dt).total_seconds() / 3600.0
        ok = lag is not None and lag <= hours
        out.append(_f(rule, r["row_key"], ok, None if lag is None else round(lag, 2), f"<= {hours}h",
                      f"{ex['field']} lag exceeds {hours}h" if not ok else "within SLA"))
    return out


def ev_arrival_sla(rule, rows, ctx):
    """Dataset-level timeliness: when did the source snapshot actually land?"""
    ex = rule["expression"]
    tolerance_h = float(ex.get("hours", 0))
    ds = rule["dataset_id"]
    svc = ctx["dataset_source"].get(ds)
    if svc is None:
        return [_f(rule, f"{ds}:partition", True, None, "no upstream arrival SLA", "dataset has no source arrival record",
                   scope="partition", partition_value=ctx["snapshot_date"])]
    snap = ctx["snapshots"].get((svc, ctx["snapshot_date"]))
    if snap is None:
        return [_f(rule, f"{ds}:partition", False, None, "arrival record present", "no arrival record for snapshot",
                   scope="partition", partition_value=ctx["snapshot_date"])]
    exp, arr = _parse_ts(snap["expected_at"]), _parse_ts(snap["arrived_at"])
    delay_h = (arr - exp).total_seconds() / 3600.0 if exp and arr else None
    ok = delay_h is not None and delay_h <= tolerance_h
    return [_f(rule, f"{svc}:{ctx['snapshot_date']}", ok, None if delay_h is None else round(delay_h, 2),
               f"<= {tolerance_h}h after expected {snap['expected_at']}",
               f"{svc} arrived {None if delay_h is None else round(delay_h, 2)}h after expected; SLA {svc}={ctx['sla_minutes'].get(svc)}min",
               scope="partition", partition_value=ctx["snapshot_date"])]


def ev_foreign_key(rule, rows, ctx):
    ex = rule["expression"]
    ref = ex["ref_dataset"]
    ref_field = ex.get("ref_field", ex["field"])
    keys = {r["payload"].get(ref_field) for r in ctx["datasets"].get(ref, [])}
    out = []
    for r in rows:
        v = r["payload"].get(ex["field"])
        out.append(_f(rule, r["row_key"], v in keys, v, f"{ref}.{ref_field}", f"{ex['field']}={v!r} has no parent in {ref}"))
    return out


def ev_sum_match(rule, rows, ctx):
    """Header (the rule's own dataset) must equal the sum of its child lines.

    ``rows`` are the header rows; ``right`` is the child dataset.  The finding
    is attributed to the header row, because that is the value a downstream
    report reads; the offending lines are visible via the header's parents.
    """
    ex = rule["expression"]
    right = ctx["datasets"].get(ex["right"], [])
    gb = ex["group_by"]
    tol = float(ex.get("tolerance", 0.0))
    sums: dict[Any, float] = {}
    for r in right:
        g = r["payload"].get(gb)
        sums[g] = sums.get(g, 0.0) + (as_float(r["payload"].get(ex["right_field"]), 0.0) or 0.0)
    out = []
    for r in rows:
        g = r["payload"].get(gb)
        header = as_float(r["payload"].get(ex["left_field"]))
        child_sum = round(sums.get(g, 0.0), 4)
        ok = header is not None and abs(child_sum - header) <= tol
        detail = f"{ex['left_field']} {header} vs sum({ex['right']}.{ex['right_field']}) {child_sum} (tol {tol})"
        out.append(_f(rule, r["row_key"], ok, header, child_sum, detail))
    return out


def ev_equals_field(rule, rows, ctx):
    ex = rule["expression"]
    out = []
    for r in rows:
        a, b = r["payload"].get(ex["field"]), r["payload"].get(ex["other"])
        out.append(_f(rule, r["row_key"], a == b, a, f"== {ex['other']} ({b})", f"{ex['field']} does not match {ex['other']}"))
    return out


def ev_row_count_drift(rule, rows, ctx):
    ex = rule["expression"]
    expected = float(ex["expected"])
    tol = float(ex.get("tolerance_pct", 5.0))
    actual = len(rows)
    drift = abs(actual - expected) / expected * 100.0 if expected else 0.0
    ok = drift <= tol
    return [_f(rule, f"{rule['dataset_id']}:partition", ok, actual, f"{expected:g} +/- {tol:g}%",
               f"row count drifted {drift:.1f}%" if not ok else "row count within tolerance",
               scope="partition", partition_value=ctx["snapshot_date"])]


def ev_volume_anomaly(rule, rows, ctx):
    ex = rule["expression"]
    minimum = int(ex["min_rows"])
    ok = len(rows) >= minimum
    return [_f(rule, f"{rule['dataset_id']}:partition", ok, len(rows), f">= {minimum}",
               "suspiciously small partition (possible partial load)" if not ok else "volume ok",
               scope="partition", partition_value=ctx["snapshot_date"])]


EVALUATORS: dict[str, Evaluator] = {
    "not_null": ev_not_null,
    "regex": ev_regex,
    "range": ev_range,
    "enum": ev_enum,
    "unique": ev_unique,
    "unique_compound": ev_unique_compound,
    "not_stale": ev_not_stale,
    "arrival_sla": ev_arrival_sla,
    "foreign_key": ev_foreign_key,
    "sum_match": ev_sum_match,
    "equals_field": ev_equals_field,
    "row_count_drift": ev_row_count_drift,
    "volume_anomaly": ev_volume_anomaly,
}


def is_partition_op(rule: dict) -> bool:
    return rule["expression"].get("op") in PARTITION_OPS


def evaluate(rule: dict, rows: list[dict], ctx: dict) -> list[dict]:
    op = rule["expression"].get("op")
    fn = EVALUATORS.get(op)
    if fn is None:
        raise ValueError(f"unknown rule op {op!r} in {rule['rule_id']}")
    return fn(rule, rows, ctx)
