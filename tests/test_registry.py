"""登记、阶段决策与结果回看测试。"""

from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from fixtures import FixtureBuilder

from retail_agent_portfolio_evidence.appraisal import build_appraisal
from retail_agent_portfolio_evidence.registry import Acceptance, Registry, RegistryError, pause_check
from retail_agent_portfolio_evidence.review import VERDICT_INSUFFICIENT, VERDICT_MET, VERDICT_MISSED


def _acceptance(**kwargs) -> Acceptance:
    defaults = dict(
        metric="unit-hours",
        direction="at-most",
        threshold=Decimal("0.025"),
        unit="hours/case",
        measured_from="2026-Q1",
        confirmed_by="业务负责人-测试",
        confirmed_on=__import__("datetime").date(2026, 11, 20),
    )
    defaults.update(kwargs)
    return Acceptance(**defaults)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_register_then_reload(self):
        registry = Registry(self.root)
        records: dict = {}
        registry.record(
            records,
            action="register",
            scenario_id="S-1",
            rationale="约定单位工时不高于 0.025",
            actor="业务负责人",
            acceptance=_acceptance(),
        )
        registry.save(records)
        reloaded = Registry(self.root).load()
        self.assertEqual(reloaded["S-1"].acceptance.metric, "unit-hours")
        self.assertEqual(reloaded["S-1"].acceptance.threshold, Decimal("0.025"))
        self.assertEqual(reloaded["S-1"].current_stage, "unregistered")

    def test_register_twice_requires_revise(self):
        registry = Registry(self.root)
        records: dict = {}
        registry.record(records, action="register", scenario_id="S-1", rationale="首次", actor="A", acceptance=_acceptance())
        with self.assertRaises(RegistryError):
            registry.record(records, action="register", scenario_id="S-1", rationale="重复", actor="A", acceptance=_acceptance())

    def test_decision_requires_rationale_and_actor(self):
        registry = Registry(self.root)
        records: dict = {}
        with self.assertRaises(RegistryError):
            registry.record(records, action="note", scenario_id="S-1", rationale="  ", actor="A")

    def test_invalid_acceptance_direction(self):
        with self.assertRaises(RegistryError):
            _acceptance(direction="maximise").validate()

    def test_release_requires_positive_amount(self):
        registry = Registry(self.root)
        with self.assertRaises(RegistryError):
            registry.record(registry.load(), action="release", scenario_id="S-1", rationale="x", actor="A", amount=Decimal("0"))

    def test_pause_preserves_released_contracts(self):
        registry = Registry(self.root)
        records: dict = {}
        registry.record(records, action="register", scenario_id="S-1", rationale="约定", actor="A", acceptance=_acceptance())
        registry.record(records, action="release", scenario_id="S-1", rationale="释放首阶段", actor="A", amount=Decimal("5000"))
        outstanding, preserved, note = pause_check(
            records["S-1"], required=Decimal("20000"), released_from_data=Decimal("0")
        )
        self.assertEqual(preserved, Decimal("5000"))
        self.assertEqual(outstanding, Decimal("15000"))
        self.assertIn("保留", note)

    def test_pause_cannot_double_count(self):
        registry = Registry(self.root)
        records: dict = {}
        registry.record(records, action="note", scenario_id="S-1", rationale="准备", actor="A")
        registry.record(records, action="pause", scenario_id="S-1", rationale="暂停", actor="A", amount=Decimal("8000"))
        outstanding, _preserved, _note = pause_check(
            records["S-1"], required=Decimal("20000"), released_from_data=Decimal("0")
        )
        self.assertEqual(outstanding, Decimal("12000"))


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _prepare(self, builder: FixtureBuilder, acceptance: Acceptance) -> None:
        builder.write(self.root)
        registry = Registry(self.root / "registry")
        records: dict = {}
        registry.record(records, action="register", scenario_id="S-1", rationale="约定", actor="A", acceptance=acceptance)
        registry.save(records)

    def test_met_when_observed_below_threshold(self):
        builder = FixtureBuilder().complete_scenario("S-1", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "60"})  # 0.02 小时/件
        self._prepare(builder, _acceptance(threshold=Decimal("0.025")))
        appraisal = build_appraisal(self.root, registry_root=self.root / "registry")
        outcome = appraisal.reviews["S-1"]
        self.assertEqual(outcome.verdict, VERDICT_MET)
        self.assertAlmostEqual(float(outcome.observed_value), 0.02, places=4)

    def test_missed_when_above_threshold(self):
        builder = FixtureBuilder().complete_scenario("S-1", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "90"})
        self._prepare(builder, _acceptance(threshold=Decimal("0.025")))
        appraisal = build_appraisal(self.root, registry_root=self.root / "registry")
        self.assertEqual(appraisal.reviews["S-1"].verdict, VERDICT_MISSED)

    def test_insufficient_when_unregistered(self):
        builder = FixtureBuilder().complete_scenario("S-1")
        builder.write(self.root)
        appraisal = build_appraisal(self.root, registry_root=None)
        self.assertEqual(appraisal.reviews["S-1"].verdict, VERDICT_INSUFFICIENT)

    def test_insufficient_when_measurement_conflicts(self):
        builder = FixtureBuilder().complete_scenario("S-1")
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "60"})
        builder.pilot("S-1", observed_volume=3000, observed_hours=90)
        self._prepare(builder, _acceptance())
        appraisal = build_appraisal(self.root, registry_root=self.root / "registry")
        self.assertEqual(appraisal.reviews["S-1"].verdict, VERDICT_INSUFFICIENT)

    def test_measured_from_excludes_earlier_periods(self):
        builder = FixtureBuilder().complete_scenario("S-1")
        builder.pilots[-1].update({"period": "2026-Q1", "observed_volume": "3000", "observed_hours": "120"})
        builder.pilot("S-1", period="2026-Q2", observed_volume=3000, observed_hours=60)
        self._prepare(builder, _acceptance(measured_from="2026-Q2", threshold=Decimal("0.03")))
        appraisal = build_appraisal(self.root, registry_root=self.root / "registry")
        # 只看 Q2 的 0.02，达标；若把 Q1 的 0.04 混入就会失败。
        self.assertEqual(appraisal.reviews["S-1"].verdict, VERDICT_MET)


if __name__ == "__main__":
    unittest.main()
