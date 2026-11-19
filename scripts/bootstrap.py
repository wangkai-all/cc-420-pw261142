"""Build a fresh demo database: schema, fixture, rule-set v1, baseline run,
one rule revision (v2) with its own run, so history is visible from the start.

    python -m scripts.bootstrap [--db data/dataq.db]

Idempotent only in the sense that it refuses to run against a non-empty DB
unless ``--force`` is given (which deletes the file).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, fixture, rules as rules_svc, ruleset, runlog
from pipeline.services import ProcessorService, RuleService


def build(path: Path, *, force: bool = False) -> dict:
    if path.exists() and path.stat().st_size > 0:
        if not force:
            print(f"refusing to overwrite {path} (use --force)", file=sys.stderr)
            return {"ok": False}
    db.init_db(path, reset=True)
    with db.session(path) as conn:
        seeded = fixture.seed(conn)
        rsid = rules_svc.create_rule_set(
            conn,
            name=ruleset.RULESET_NAME,
            created_by="bootstrap",
            weights=ruleset.WEIGHTS,
            thresholds=ruleset.THRESHOLDS,
            rules=ruleset.rules(),
            notes="v1 baseline: equal emphasis, default thresholds",
        )
        runlog.event(conn, "bootstrap.seeded", actor="bootstrap", payload=seeded)
    conn = db.connect(path)
    proc, rules = ProcessorService(conn), RuleService(conn)
    try:
        base_run = proc.create_run(kind="full", actor="bootstrap", rule_set_id=rsid)
        base = proc.execute(base_run)
        # Lay down a second version so history and the (immutable) v1 scores are
        # visible from the first page load — the demo then revises on top of it.
        v2 = rules.revise(
            rsid, actor="bootstrap",
            weights={**ruleset.WEIGHTS, "validity": 0.30},
            notes="v2: validity risk weighted up after the Q3 close review",
        )
        run2 = proc.create_run(kind="full", actor="bootstrap", rule_set_id=v2["id"])
        second = proc.execute(run2)
    finally:
        conn.close()
    return {"ok": True, "db": str(path), "rule_set_id": rsid, "base_run": base,
            "rule_set_v2": v2["id"], "second_run": second}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    out = build(Path(args.db), force=args.force)
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
