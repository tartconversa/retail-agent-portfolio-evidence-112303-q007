"""证据层测试：期间换算、矛盾测量、缺基线、观察方向。"""

from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from fixtures import FixtureBuilder

from retail_agent_portfolio_evidence.appraisal import build_appraisal
from retail_agent_portfolio_evidence.evidence import (
    TIER_CONFLICTED,
    TIER_MISSING_BASELINE,
    TIER_OBSERVED,
    TIER_PARTIAL,
    assess_evidence,
    period_months,
)
from retail_agent_portfolio_evidence.loader import load_dataset
from retail_agent_portfolio_evidence.policy import load_policy


class PeriodParsingTests(unittest.TestCase):
    def test_quarter_and_month(self):
        self.assertEqual(period_months("2026-Q2"), 3)
        self.assertEqual(period_months("2026-11"), 1)
        self.assertIsNone(period_months("2026"))
        self.assertIsNone(period_months("2026-Q9"))
        self.assertIsNone(period_months("2026-13"))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.policy = load_policy()

    def _appraise(self, builder: FixtureBuilder):
        builder.write(self.root)
        dataset, _issues = load_dataset(self.root)
        scenario = next(dataset.iter_scenarios())
        return scenario, dataset, assess_evidence(scenario, dataset, self.policy)

    def test_quarter_is_normalised_to_monthly_basis(self):
        # 季度业务量 3000、工时 60 → 月度 1000、20；基线月度 1000、30 → 节省 1/3。
        builder = FixtureBuilder().complete_scenario("S-1", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"period": "2026-Q2", "observed_volume": "3000", "observed_hours": "60"})
        scenario, _dataset, evidence = self._appraise(builder)
        self.assertEqual(evidence.tier, TIER_OBSERVED)
        self.assertNotIn("SCOPE_VOLUME_MISMATCH", evidence.flags)
        self.assertIn("PERIOD_BASIS_MISMATCH", evidence.flags)
        self.assertTrue(evidence.observed_monetizable)
        self.assertAlmostEqual(float(evidence.observed_delta_best), 1 / 3, places=4)

    def test_scope_mismatch_blocks_monetisation(self):
        # 季度 600 件 → 月度 200，远低于月度基线 1000，不可外推。
        builder = FixtureBuilder().complete_scenario("S-1", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"observed_volume": "600", "observed_hours": "12", "period": "2026-Q2"})
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertIn("SCOPE_VOLUME_MISMATCH", evidence.flags)
        self.assertFalse(evidence.observed_monetizable)
        self.assertEqual(evidence.observed_annual_value_conservative, evidence.observed_annual_value_conservative)
        self.assertGreater(evidence.observed_annual_value_conservative, 0)

    def test_conflicting_measurements_are_not_averaged(self):
        builder = FixtureBuilder().complete_scenario("S-1", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "60"})
        builder.pilot(
            "S-1", period="2026-Q2", observed_volume=3000, observed_hours=90,
            measurement_state="complete", source_version="MV-1",
        )
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertEqual(evidence.tier, TIER_CONFLICTED)
        self.assertIn("CONFLICTING_MEASUREMENTS", evidence.flags)
        # 保守取值取较高的单位工时（较大工时 90/3000）。
        self.assertAlmostEqual(float(evidence.observed_unit_hours_conservative), 0.03, places=4)

    def test_baseline_missing_downgrades_tier(self):
        builder = FixtureBuilder().complete_scenario("S-1")
        builder.pilots[-1]["measurement_state"] = "baseline-missing"
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertEqual(evidence.tier, TIER_MISSING_BASELINE)
        self.assertFalse(evidence.observed_monetizable)

    def test_partial_measurement_downgrades_tier(self):
        builder = FixtureBuilder().complete_scenario("S-1")
        builder.pilots[-1]["measurement_state"] = "partial"
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertEqual(evidence.tier, TIER_PARTIAL)
        self.assertFalse(evidence.observed_monetizable)

    def test_observed_worse_than_baseline_is_flagged_not_clamped(self):
        # 季度 3000 件 / 120 小时 → 0.04 小时/件，高于基线 30/1000=0.03。
        builder = FixtureBuilder().complete_scenario("S-1", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "120"})
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertIn("OBSERVED_WORSE_THAN_BASELINE", evidence.flags)
        self.assertGreater(evidence.observed_delta_best, -1)
        self.assertLess(evidence.observed_delta_best, 0)
        self.assertEqual(evidence.assumption_status, "contradicted")

    def test_no_pilot_records(self):
        builder = FixtureBuilder().scenario("S-1").dependency("S-1").review("S-1").stage("S-1")
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertEqual(evidence.tier, "no_measurement")
        self.assertIsNone(evidence.observed_annual_value_conservative)

    def test_zero_baseline_hours_invalidates(self):
        builder = FixtureBuilder().complete_scenario("S-1", baseline_hours=0)
        _scenario, _dataset, evidence = self._appraise(builder)
        self.assertEqual(evidence.tier, "invalid")
        self.assertIsNone(evidence.baseline_unit_hours)


class AppraisalEndToEndTests(unittest.TestCase):
    def test_portfolio_selects_eligible_and_explains_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = FixtureBuilder()
            # 高质量场景：已观察、治理通过、就绪、有未释放资金。
            builder.complete_scenario("S-GOOD", monthly_volume=1000, baseline_hours=30)
            builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "60"})
            builder.stage("S-GOOD", amount=5000, release_condition="pilot-target", committee_decision="held")
            # 业务方未确认，不可入选。
            builder.complete_scenario("S-PAUSED", owner_state="paused")
            # 治理要求修订，不可入选。
            builder.scenario("S-REVISE")
            builder.dependency("S-REVISE")
            builder.review("S-REVISE", decision="revise")
            builder.stage("S-REVISE")
            builder.pilot("S-REVISE", observed_volume=3000, observed_hours=60)
            builder.write(Path(tmp))
            appraisal = build_appraisal(tmp, registry_root=None)
        self.assertIn("S-GOOD", appraisal.portfolio.selected_ids)
        self.assertNotIn("S-PAUSED", appraisal.portfolio.selected_ids)
        self.assertNotIn("S-REVISE", appraisal.portfolio.selected_ids)
        reasons = {val.scenario_id: reason for val, reason in appraisal.portfolio.excluded}
        self.assertIn("业务负责人承诺", reasons["S-PAUSED"])
        self.assertIn("数据治理结论", reasons["S-REVISE"])

    def test_advance_eligible_requires_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder = FixtureBuilder().complete_scenario("S-1")
            builder.write(Path(tmp))
            without = build_appraisal(tmp, registry_root=None)
            self.assertTrue(without.valuations["S-1"].entry_eligible)
            self.assertFalse(without.valuations["S-1"].advance_eligible)
        self.assertFalse(without.reviews["S-1"].verdict != "insufficient")


if __name__ == "__main__":
    unittest.main()
