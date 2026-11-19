"""Run bookkeeping: step log, event log, interruption + resume semantics.

Steps are the unit of checkpointing.  A run is executed step by step, each in
its own committed transaction, so an interrupt leaves a truthful partial
result: completed steps say ``done``, the faulting step says
``interrupted``, later steps say ``pending``, and the run says ``interrupted``.
Nothing is rolled back to make the run *look* clean.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

STEPS = [
    (1, "prepare", "resolve active rule set and scope"),
    (2, "ingest", "verify source arrival snapshots"),
    (3, "transform", "rebuild affected datasets"),
    (4, "evaluate", "run quality rules"),
    (5, "trace", "propagate findings along lineage"),
    (6, "score", "aggregate dimension scores"),
    (7, "reports", "materialise report impact and cost"),
    (8, "finalize", "close the run"),
]
STEP_NAMES = {n: name for n, name, _ in STEPS}
STEP_NO = {name: n for n, name, _ in STEPS}


class Interrupted(Exception):
    """Raised by the runner when a simulated fault is requested."""

    def __init__(self, step: str, detail: str):
        super().__init__(detail)
        self.step = step
        self.detail = detail


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event(conn, action: str, *, actor: str = "system", subject: str | None = None, payload: Any = None) -> None:
    conn.execute(
        "INSERT INTO events(id,at,actor,action,subject,payload) VALUES(?,?,?,?,?,?)",
        (f"ev_{uuid.uuid4().hex[:12]}", now(), actor, action, subject, json.dumps(payload, sort_keys=True) if payload is not None else None),
    )


def initialise_steps(conn, run_id: str) -> None:
    conn.executemany(
        "INSERT INTO run_steps(run_id,step_no,name,status,detail) VALUES(?,?,?,?,?)",
        [(run_id, n, name, "pending", detail) for n, name, detail in STEPS],
    )


def mark(conn, run_id: str, step: str, status: str, detail: str | None = None) -> None:
    n = STEP_NO[step]
    row = conn.execute("SELECT status FROM run_steps WHERE run_id=? AND step_no=?", (run_id, n)).fetchone()
    fields = ["status=?"]
    args: list[Any] = [status]
    if status == "running":
        fields.append("started_at=?")
        args.append(now())
    elif status in ("done", "failed", "interrupted", "reused", "skipped"):
        fields.append("finished_at=?")
        args.append(now())
    if detail is not None:
        fields.append("detail=?")
        args.append(detail)
    args.extend([run_id, n])
    if row is None:
        conn.execute(
            "INSERT INTO run_steps(run_id,step_no,name,status,started_at,detail) VALUES(?,?,?,?,?,?)",
            (run_id, n, step, status, now() if status != "pending" else None, detail),
        )
    else:
        conn.execute(f"UPDATE run_steps SET {', '.join(fields)} WHERE run_id=? AND step_no=?", args)


def interrupted_steps(conn, run_id: str) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM run_steps WHERE run_id=? AND status IN ('interrupted','pending') ORDER BY step_no", (run_id,)
    )]


def step_status(conn, run_id: str, step: str) -> str | None:
    row = conn.execute("SELECT status FROM run_steps WHERE run_id=? AND step_no=?", (run_id, STEP_NO[step])).fetchone()
    return row["status"] if row else None


def completed_steps(conn, run_id: str) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM run_steps WHERE run_id=? AND status IN ('done','reused')", (run_id,)
    )}


def run_summary(conn, run_id: str) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(run_id)
    d = dict(run)
    d["scope"] = json.loads(d["scope"]) if d["scope"] else {}
    d["steps"] = [dict(r) for r in conn.execute("SELECT * FROM run_steps WHERE run_id=? ORDER BY step_no", (run_id,))]
    return d
