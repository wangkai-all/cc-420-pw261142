"""End-to-end checks for the Lumen Kits quality pipeline.

    python -m unittest discover -s tests -v

Every test builds its own throwaway database from the same bootstrap path the
demo uses, so the assertions run against produced artefacts rather than mocks.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import db, runlog
from pipeline.services import ProcessorService, ReportService, RuleService
from scripts.bootstrap import build


_TEMPLATE: tempfile.TemporaryDirectory | None = None


def template_db() -> Path:
    """One bootstrap per test session; each test copies the file.

    Revision and fix tests mutate the database, so they cannot share a file,
    but rebuilding from scratch 20 times is just slow.
    """
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = tempfile.TemporaryDirectory()
        path = Path(_TEMPLATE.name) / "template.db"
        assert build(path, force=True)["ok"]
    return Path(_TEMPLATE.name) / "template.db"


class Base(unittest.TestCase):
    """Each test gets a private database: revision and fix tests mutate state."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "q.db"
        src = template_db()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(src) + suffix)
            if f.exists():
                shutil.copyfile(f, Path(str(self.path) + suffix))
        self.conn = db.connect(self.path)
        self.built = self._built()
        self.proc = ProcessorService(self.conn)
        self.rules = RuleService(self.conn)
        self.reports = ReportService(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _built(self) -> dict:
        base = self.conn.execute(
            "SELECT id FROM runs WHERE kind='full' AND status='completed' ORDER BY started_at LIMIT 1"
        ).fetchone()
        return {"base_run": {"run_id": base["id"] if base else None},
                "rule_set_id": self.conn.execute(
                    "SELECT id FROM rule_sets ORDER BY version LIMIT 1").fetchone()["id"]}

    @property
    def base_run(self) -> str:
        return self.built["base_run"]["run_id"]

    def run_full(self, rule_set_id: str | None = None) -> str:
        rid = self.proc.create_run(kind="full", actor="test", rule_set_id=rule_set_id)
        out = self.proc.execute(rid)
        self.assertEqual(out["status"], "completed", out)
        return rid


class TestRuleVersioning(Base):
    def test_revision_creates_child_and_supersedes_parent(self):
        parent = self.rules.active()
        before = {r["rule_id"]: dict(r) for r in self.rules.get(parent["id"])["rules"]}
        child = self.rules.revise(parent["id"], actor="test", weights={**parent["weights"], "validity": 0.41})
        self.assertNotEqual(parent["id"], child["id"])
        self.assertEqual(self.rules.get(parent["id"])["status"], "superseded")
        self.assertEqual(self.rules.get(child["id"])["status"], "active")
        self.assertEqual(child["parent_id"], parent["id"])
        # parent rows are byte-for-byte unchanged
        after = {r["rule_id"]: dict(r) for r in self.rules.get(parent["id"])["rules"]}
        self.assertEqual(before, after)
        # the new version stores the weight as entered
        weights = self.rules.get(child["id"])["weights"]
        self.assertAlmostEqual(weights["validity"], 0.41, places=6)

    def test_revision_does_not_touch_historical_scores(self):
        parent = self.rules.active()
        before = self.rules.snapshot_scores(parent["id"])
        self.assertTrue(before)
        child = self.rules.revise(parent["id"], actor="test", thresholds=None)
        self.run_full(child["id"])
        after = self.rules.snapshot_scores(parent["id"])
        self.assertEqual(before, after)
        # and the child's scores are a different, complete set
        fresh = self.rules.snapshot_scores(child["id"])
        self.assertTrue(fresh)
        self.assertNotEqual({s["run_id"] for s in fresh}, {s["run_id"] for s in before})

    def test_history_keeps_one_active_version(self):
        versions = self.rules.history()
        self.assertEqual(sum(1 for v in versions if v["status"] == "active"), 1)
        versions = sorted(versions, key=lambda v: v["version"])
        for prev, nxt in zip(versions, versions[1:]):
            self.assertEqual(nxt["parent_id"], prev["id"])


class TestScoresDrillDown(Base):
    def test_every_score_row_is_explainable(self):
        rows = self.reports.dataset_detail(self.base_run, "stg_customer")["scores"]
        self.assertTrue(rows)
        for s in rows:
            self.assertTrue(s["formula"], s)
            self.assertIsNotNone(s["score"])
            if s["dimension"] != "overall":
                self.assertGreater(s["evaluated"], 0)

    def test_overall_is_weighted_over_participating_dimensions(self):
        d = self.reports.dataset_detail(self.base_run, "stg_customer")
        overall = next(x for x in d["scores"] if x["dimension"] == "overall")
        dims = [x for x in d["scores"] if x["dimension"] != "overall"]
        weights = self.rules.get(self.built["rule_set_id"])["weights"]
        num = sum(x["score"] * weights[x["dimension"]] for x in dims)
        den = sum(weights[x["dimension"]] for x in dims)
        self.assertAlmostEqual(overall["score"], num / den, places=2)

    def test_score_links_to_facts(self):
        d = self.reports.dataset_detail(self.base_run, "stg_customer")
        self.assertTrue(d["violations"])
        factored = [v for v in d["violations"] if v.get("root_fact")]
        self.assertTrue(factored, "at least one violation must resolve to a source record")
        fact = factored[0]["root_fact"]
        self.assertIn("payload", fact)
        self.assertIn("record_id", fact)

    def test_empty_dimension_is_not_scored_as_100(self):
        for ds in self.reports.scorecard(self.base_run)["datasets"]:
            dims = set(ds["dimensions"])
            self.assertLessEqual(dims, {"completeness", "consistency", "timeliness", "validity", "uniqueness"})
            if ds.get("overall") is not None and not dims:
                self.fail(f"{ds['dataset_id']} has an overall score with no dimension")


class TestImpact(Base):
    def test_material_vs_traced_differs_by_consumer(self):
        arts = {a["report_id"]: a for a in self.reports.impacts(self.base_run)}
        fin, execd = arts["rpt_finance_close"], arts["rpt_exec_daily"]
        self.assertGreater(fin["material_rows"], execd["material_rows"])
        self.assertGreater(fin["traced_rows"], fin["material_rows"])
        self.assertGreater(fin["impact_weight"], execd["impact_weight"])

    def test_all_four_impact_states_are_reachable(self):
        states = {a["impact_status"] for a in self.reports.impacts(self.base_run)}
        self.assertTrue({"measured", "estimated", "traced_only", "unknown"} <= states, states)

    def test_unknown_lineage_beats_clear(self):
        arts = {a["report_id"]: a for a in self.reports.impacts(self.base_run)}
        treasury = arts["rpt_treasury"]
        self.assertEqual(treasury["impact_status"], "unknown")
        self.assertTrue(treasury["blind_spots"])
        self.assertIn("high", [g["severity"] for g in treasury["blind_spots"]])

    def test_measured_hits_are_row_scoped_and_carry_a_path(self):
        rows = self.conn.execute(
            "SELECT * FROM lineage_hits WHERE run_id=? AND status='measured' LIMIT 20", (self.base_run,)
        ).fetchall()
        self.assertTrue(rows)
        for h in rows:
            self.assertTrue(h["path"])
            self.assertEqual(h["scope"], "row")

    def test_partition_estimates_are_separate_from_row_hits(self):
        hits = self.conn.execute(
            "SELECT COUNT(*) FROM lineage_hits WHERE run_id=? AND status='estimated'", (self.base_run,)
        ).fetchone()[0]
        est = self.conn.execute(
            "SELECT COUNT(*) FROM impact_estimates WHERE run_id=?", (self.base_run,)
        ).fetchone()[0]
        self.assertGreater(est, 0)
        self.assertEqual(hits, 0, "row hits never masquerade as estimates")
        for e in self.conn.execute("SELECT * FROM impact_estimates WHERE run_id=?", (self.base_run,)):
            self.assertTrue(e["method"])
            self.assertTrue(e["explanation"])
            self.assertIsNotNone(e["estimated_rows"])

    def test_cost_is_always_labelled_an_estimate(self):
        for a in self.reports.impacts(self.base_run):
            self.assertEqual(a["cost_kind"], "estimate")
            self.assertTrue(a["cost_basis"])

    def test_same_rule_has_different_materiality_by_report(self):
        # pick a rule on stg_customer that reaches several reports
        rows = self.conn.execute(
            "SELECT impacted_node, material FROM lineage_hits "
            "WHERE run_id=? AND root_dataset='stg_customer' AND node_type='report'",
            (self.base_run,),
        ).fetchall()
        self.assertTrue(rows)
        by_report: dict[str, set[int]] = {}
        for r in rows:
            by_report.setdefault(r["impacted_node"], set()).add(r["material"])
        self.assertTrue(any(0 in v for v in by_report.values()))
        self.assertTrue(any(1 in v for v in by_report.values()))


class TestRecovery(Base):
    def test_interrupt_and_resume_never_fabricate_steps(self):
        rid = self.proc.simulate_interrupt(at_step="evaluate", detail="test crash")
        summary = runlog.run_summary(self.conn, rid)
        self.assertEqual(summary["status"], "interrupted")
        interrupted = [s for s in summary["steps"] if s["status"] == "interrupted"]
        self.assertEqual([s["name"] for s in interrupted], ["evaluate"])
        before = {s["name"]: s["status"] for s in summary["steps"]}

        out = self.proc.resume(rid)
        self.assertEqual(out["status"], "completed", out)
        after = runlog.run_summary(self.conn, rid)
        self.assertEqual(after["status"], "completed")
        # completed steps are marked reused, never re-run; the crashed step runs now
        states = {s["name"]: s["status"] for s in after["steps"]}
        for name, was in before.items():
            if was == "done":
                self.assertEqual(states[name], "reused", name)
        self.assertEqual(states["evaluate"], "done")
        reused_detail = {s["name"]: s["detail"] for s in after["steps"] if s["status"] == "reused"}
        self.assertTrue(all("already complete" in (v or "") for v in reused_detail.values()))
        artifacts = self.conn.execute(
            "SELECT COUNT(*) FROM report_artifacts WHERE run_id=?", (rid,)
        ).fetchone()[0]
        self.assertEqual(artifacts, len({a["report_id"] for a in self.reports.impacts(rid)}))

    def test_interrupt_at_each_step_resumes_cleanly(self):
        for step in ("transform", "trace", "reports"):
            with self.subTest(step=step):
                rid = self.proc.simulate_interrupt(at_step=step)
                self.assertEqual(runlog.run_summary(self.conn, rid)["status"], "interrupted")
                self.assertEqual(self.proc.resume(rid)["status"], "completed")

    def test_completed_run_cannot_be_resumed(self):
        with self.assertRaises(Exception):
            self.proc.resume(self.base_run)


class TestTargetedRecompute(Base):
    def test_fix_recomputes_only_downstream_closure(self):
        before = self.reports.scorecard(self.base_run)
        base_scores = {(d["dataset_id"]): d["overall"] for d in before["datasets"]}
        fixed = self.proc.fix_and_rerun(
            source_id="src_crm",
            record_ids=["crm:0007", "crm:0019"],
            patches=[{"email": "t7@example-smb.test"}, {"email": "t19@example-mid.test"}],
        )
        scope = set(fixed["recomputed_datasets"])
        self.assertEqual(scope, {"raw_customer", "stg_customer", "dim_customer", "mart_customer_health"})
        rid = fixed["run_id"]
        mid = {(d["dataset_id"]): d["overall"] for d in self.reports.scorecard(fixed["base_run_id"])["datasets"]}
        after = self.reports.scorecard(rid)
        # every dataset outside the closure reuses the base run verbatim
        for d in after["datasets"]:
            if d["dataset_id"] not in scope:
                self.assertEqual(d["overall"], mid[d["dataset_id"]], d["dataset_id"])
        # the repaired dataset improved and cites the base run for reused rules
        stg = next(d for d in after["datasets"] if d["dataset_id"] == "stg_customer")
        self.assertGreater(stg["overall"], base_scores["stg_customer"])
        reused = self.conn.execute(
            "SELECT COUNT(*) FROM scores WHERE run_id=? AND reused_from IS NOT NULL", (rid,)
        ).fetchone()[0]
        self.assertGreater(reused, 0)

    def test_fix_leaves_other_source_records_alone(self):
        fixed = self.proc.fix_and_rerun(
            source_id="src_crm",
            record_ids=["crm:0033"],
            patches=[{"email": "only33@example-mid.test"}],
        )
        row = self.conn.execute(
            "SELECT payload, fixed_at FROM source_records WHERE source_id='src_crm' AND record_id='crm:0007'"
        ).fetchone()
        self.assertIsNone(row["fixed_at"])
        self.assertIsNone(json.loads(row["payload"]).get("email"))


class TestValidation(unittest.TestCase):
    def test_ruleset_has_unique_ids_and_known_dimensions(self):
        from pipeline import ruleset

        ids = [r["rule_id"] for r in ruleset.rules()]
        self.assertEqual(len(ids), len(set(ids)))
        for r in ruleset.rules():
            self.assertIn(r["dimension"], {"completeness", "consistency", "timeliness", "validity", "uniqueness"})
            self.assertIn(r["severity"], {"low", "medium", "high"})
            self.assertIn(r["materiality"], {"critical", "advisory"})
            self.assertTrue(r.get("description"), r)


if __name__ == "__main__":
    unittest.main()
