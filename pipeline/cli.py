"""Command line for the platform.  Every UI action has a CLI equivalent.

    python -m pipeline.cli bootstrap --force
    python -m pipeline.cli status
    python -m pipeline.cli scorecard [--run RUN]
    python -m pipeline.cli dataset stg_customer [--run RUN]
    python -m pipeline.cli impacts [--run RUN]
    python -m pipeline.cli rules
    python -m pipeline.cli revise --weight validity=0.35 --note "raise validity"
    python -m pipeline.cli run [--kind full] [--snapshot 2026-11-18]
    python -m pipeline.cli fix --source src_crm --record crm:0007 --set email=user7@example-smb.test
    python -m pipeline.cli interrupt --step evaluate
    python -m pipeline.cli resume RUN
    python -m pipeline.cli serve --port 8787
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, runlog
from pipeline.services import ProcessorService, ReportService, RuleService


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _run_or_latest(rs: ReportService, run_id: str | None) -> str:
    rid = run_id or rs.latest_run(include_interrupted=True)
    if not rid:
        raise SystemExit("no run yet; run `bootstrap` first")
    return rid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dataq", description="lineage-aware data quality platform")
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="build a fresh demo database")
    b.add_argument("--force", action="store_true")

    sub.add_parser("status", help="active rule set + recent runs")

    sc = sub.add_parser("scorecard", help="dataset scores for a run")
    sc.add_argument("--run")

    ds = sub.add_parser("dataset", help="drill a dataset score down to facts")
    ds.add_argument("dataset_id")
    ds.add_argument("--run")

    im = sub.add_parser("impacts", help="report impact + cost")
    im.add_argument("--run")

    sub.add_parser("rules", help="list rule-set versions")

    rv = sub.add_parser("revise", help="create a new rule-set version")
    rv.add_argument("--weight", action="append", default=[], metavar="DIM=VALUE")
    rv.add_argument("--threshold", action="append", default=[], metavar="DIM=VALUE")
    rv.add_argument("--severity", action="append", default=[], metavar="RULE=SEV")
    rv.add_argument("--actor", default="cli")
    rv.add_argument("--note")

    rn = sub.add_parser("run", help="start and execute a quality run")
    rn.add_argument("--kind", default="full")
    rn.add_argument("--snapshot")
    rn.add_argument("--ruleset")

    fx = sub.add_parser("fix", help="fix upstream records, then targeted recompute")
    fx.add_argument("--source", required=True)
    fx.add_argument("--record", action="append", required=True)
    fx.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")
    fx.add_argument("--note", default="upstream fix")

    it = sub.add_parser("interrupt", help="simulate a crash at a step")
    it.add_argument("--step", default="evaluate")
    it.add_argument("--snapshot")

    rs_ = sub.add_parser("resume", help="resume an interrupted run")
    rs_.add_argument("run_id")

    lv = sub.add_parser("lineage", help="show the lineage graph")
    lv.add_argument("dataset_id", nargs="?")

    er = sub.add_parser("errors", help="list violations")
    er.add_argument("--run")
    er.add_argument("--dataset")
    er.add_argument("--rule")

    ev = sub.add_parser("events", help="audit log")
    ev.add_argument("--limit", type=int, default=30)

    sv = sub.add_parser("serve", help="start the web UI + JSON API")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--host", default="127.0.0.1")

    args = ap.parse_args(argv)
    path = Path(args.db)

    if args.cmd == "bootstrap":
        from scripts import bootstrap
        _print(bootstrap.build(path, force=args.force))
        return 0

    if not path.exists():
        raise SystemExit(f"database {path} does not exist; run `bootstrap` first")

    if args.cmd == "serve":
        from pipeline.api import serve
        serve(host=args.host, port=args.port, db_path=path)
        return 0

    with db.session(path) as conn:
        rules_svc, proc, reports = RuleService(conn), ProcessorService(conn), ReportService(conn)

        if args.cmd == "status":
            _print({
                "active_rule_set": rules_svc.active(),
                "runs": proc.runs(10),
                "latest": reports.latest_run(include_interrupted=True),
            })
        elif args.cmd == "scorecard":
            _print(reports.scorecard(_run_or_latest(reports, args.run)))
        elif args.cmd == "dataset":
            _print(reports.dataset_detail(_run_or_latest(reports, args.run), args.dataset_id))
        elif args.cmd == "impacts":
            _print(reports.impacts(_run_or_latest(reports, args.run)))
        elif args.cmd == "rules":
            _print(rules_svc.list_rule_sets())
        elif args.cmd == "revise":
            parent = rules_svc.active()
            if not parent:
                raise SystemExit("no active rule set")
            weights = dict(parent["weights"])
            for item in args.weight:
                k, v = item.split("=", 1)
                weights[k] = float(v)
            thresholds = dict(parent["thresholds"])
            for item in args.threshold:
                k, v = item.split("=", 1)
                thresholds[k] = float(v)
            patches = {}
            for item in args.severity:
                rid, sev = item.split("=", 1)
                patches[rid] = {"severity": sev}
            _print(rules_svc.revise(parent["id"], actor=args.actor, weights=weights,
                                    thresholds=thresholds, rule_patches=patches, notes=args.note))
        elif args.cmd == "run":
            rid = proc.create_run(kind=args.kind, actor="cli", rule_set_id=args.ruleset,
                                  snapshot_date=args.snapshot)
            _print(proc.execute(rid))
        elif args.cmd == "fix":
            patch = {}
            for item in args.set:
                k, v = item.split("=", 1)
                try:
                    patch[k] = json.loads(v)
                except json.JSONDecodeError:
                    patch[k] = v
            _print(proc.fix_and_rerun(source_id=args.source, record_ids=args.record,
                                      patches=[patch] * len(args.record), note=args.note))
        elif args.cmd == "interrupt":
            rid = proc.simulate_interrupt(at_step=args.step, snapshot_date=args.snapshot)
            _print(runlog.run_summary(conn, rid))
        elif args.cmd == "resume":
            _print(proc.resume(args.run_id))
        elif args.cmd == "lineage":
            _print(reports.lineage(args.dataset_id))
        elif args.cmd == "errors":
            _print(reports.errors(_run_or_latest(reports, args.run), dataset_id=args.dataset, rule_id=args.rule))
        elif args.cmd == "events":
            _print([dict(r) for r in conn.execute(
                "SELECT * FROM events ORDER BY at DESC, rowid DESC LIMIT ?", (args.limit,))])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
