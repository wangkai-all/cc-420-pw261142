"""Reproducible demo for the walkthrough in README.md.

    python -m scripts.demo [--db data/demo.db] [--keep]

Runs the whole story end to end and prints a compact transcript:

  1. baseline run (rule set v1)
  2. interrupt the next run mid-flight, show the honest partial state, resume
  3. revise weights -> new rule-set version, run again, old scores untouched
  4. drill one score down to the original fact and its downstream reports
  5. fix upstream records -> targeted recompute of only the affected slice
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db, runlog
from pipeline.services import ProcessorService, ReportService, RuleService
from scripts.bootstrap import build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(db.DEFAULT_DB).with_name("demo.db")))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.db)
    out = build(path, force=True)
    print(f"[bootstrap] rule set {out['rule_set_id']} · baseline run {out['base_run']['run_id']}")
    print(f"            history already seeded to v2 ({out['rule_set_v2']}) so versioning is visible immediately")
    print(f"            {out['base_run']['detail']}\n")

    conn = db.connect(path)
    try:
        proc, rules, reports = ProcessorService(conn), RuleService(conn), ReportService(conn)
        base = out["base_run"]["run_id"]

        print("[scorecard · baseline]")
        for d in reports.scorecard(base)["datasets"]:
            dims = " ".join(f"{k[:4]}={v['score']:.1f}{'' if v.get('reused_from') is None else '*'}" for k, v in d["dimensions"].items())
            print(f"  {d['dataset_id']:20s} overall={d['overall']:6.2f}  {dims}")

        print("\n[interrupt] crash while entering 'evaluate'")
        rid = proc.simulate_interrupt(at_step="evaluate", detail="worker pod evicted (demo)")
        summary = runlog.run_summary(conn, rid)
        print(f"  run {rid} -> {summary['status']}")
        for s in summary["steps"]:
            print(f"    {s['step_no']}. {s['name']:10s} {s['status']:11s} {s['detail'] or ''}")
        print("  resuming…")
        res = proc.resume(rid)
        print(f"  -> {res['status']}: {res['detail']}\n")

        parent = rules.active()
        print(f"[revise] active v{parent['version']}: validity weight {parent['weights']['validity']:.2f} -> 0.35")
        before = rules.snapshot_scores(parent["id"])
        child = rules.revise(parent["id"], actor="analyst", weights={**parent["weights"], "validity": 0.35},
                             notes="quality review: validity carries more risk")
        print(f"  v{parent['version']} -> v{child['version']} ({child['id']}); "
              f"parent v{parent['version']} is now {rules.get(parent['id'])['status']}, child is {rules.get(child['id'])['status']}")
        run2 = proc.create_run(kind="full", actor="analyst", rule_set_id=child["id"])
        print(f"  run {run2}: {proc.execute(run2)['detail']}")
        after_revise = rules.snapshot_scores(parent["id"])
        print(f"  v{parent['version']} scores untouched: {len(after_revise)} rows, identical={after_revise == before}")
        hist = reports.score_history("stg_customer")
        print("  stg_customer history:", [(h["rule_set_version"], round(h["score"], 2)) for h in hist], "\n")

        print("[drill] stg_customer · worst rule → fact → downstream")
        detail = reports.dataset_detail(run2, "stg_customer")
        with_viol = [r for r in detail["rules"] if r["violations"]]
        worst = next((v for v in detail["violations"] if v["rule_id"] == with_viol[0]["rule_id"]), None)
        print(f"  {with_viol[0]['rule_id']} ({with_viol[0]['dimension']}, {with_viol[0]['materiality']}): "
              f"{with_viol[0]['violations']} of {with_viol[0]['evaluated']} checks failed")
        print("  expanded finding → source fact + downstream:")
        print("  ", json.dumps({k: worst[k] for k in ("rule_id", "record_id", "field", "actual", "expected")}, default=str))
        print("   root_fact:", json.dumps(worst["root_fact"]["payload"], default=str)[:220])
        print("   downstream:", [(d["impacted_node"], d["depth"], "material" if d["material"] else "touched")
                                 for d in worst["downstream"]], "\n")

        print("[downstream] same error, different consumers (measured / estimated / traced / unknown)")
        for a in reports.impacts(run2):
            print(f"  {a['report_id']:22s} {a['impact_status']:10s} rows={a['material_rows']:3d} traced={a['traced_rows']:3d} "
                  f"cost=${a['cost_usd']:.4f} ({a['cost_kind']}) blind={len(a['blind_spots'])}")

        print("\n[fix] repair two CRM emails, targeted recompute")
        fixed = proc.fix_and_rerun(source_id="src_crm", record_ids=["crm:0007", "crm:0019"],
                                   patches=[{"email": "fixed7@example-smb.test"}, {"email": "fixed19@example-mid.test"}],
                                   note="CRM backfill")
        fresh = [r["rule_id"] for r in reports.dataset_detail(fixed["run_id"], "stg_customer")["rules"]
                 if not r.get("reused_from")]
        print(f"  recomputed only: {fixed['recomputed_datasets']}")
        print(f"  rules actually re-evaluated on stg_customer: {fresh or 'none (all reused)'}")
        print(f"  {fixed['detail']}")
        after = reports.scorecard(fixed["run_id"])
        for d in after["datasets"]:
            if d["dataset_id"] in fixed["recomputed_datasets"]:
                print(f"  {d['dataset_id']:20s} overall={d['overall']:6.2f} violations={d['violations']}")
    finally:
        conn.close()
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(path) + suffix)
            if f.exists():
                f.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
