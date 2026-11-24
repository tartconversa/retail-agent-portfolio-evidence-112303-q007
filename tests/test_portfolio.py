"""组合选择测试：预算、共享容量、确定性、敏感性。"""

from dataclasses import replace
from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from fixtures import FixtureBuilder

from retail_agent_portfolio_evidence.appraisal import build_appraisal
from retail_agent_portfolio_evidence.policy import load_policy
from retail_agent_portfolio_evidence.portfolio import select_portfolio


def _appraise(builder: FixtureBuilder, tmp: str):
    builder.write(Path(tmp))
    return build_appraisal(tmp, registry_root=None)


class PortfolioTests(unittest.TestCase):
    def _base_builder(self) -> FixtureBuilder:
        builder = FixtureBuilder()
        builder.complete_scenario("S-A", monthly_volume=1000, baseline_hours=30)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "60"})
        builder.complete_scenario("S-B", monthly_volume=800, baseline_hours=20)
        builder.pilots[-1].update({"observed_volume": "2400", "observed_hours": "48"})
        return builder

    def test_budget_binding_chooses_highest_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            appraisal = _appraise(self._base_builder(), tmp)
        policy = appraisal.policy
        valuations = appraisal.ordered_valuations()
        # 每个启动成本 20000，预算只够一个。
        portfolio = select_portfolio(valuations, appraisal.dataset, policy, budget=Decimal("25000"))
        self.assertEqual(len(portfolio.selected), 1)
        best = max(valuations, key=lambda v: v.risk_adjusted_annual_value)
        self.assertEqual(portfolio.selected_ids, [best.scenario_id])
        self.assertLessEqual(portfolio.total_cost, Decimal("25000"))

    def test_no_overspend(self):
        with tempfile.TemporaryDirectory() as tmp:
            appraisal = _appraise(self._base_builder(), tmp)
        portfolio = select_portfolio(
            appraisal.ordered_valuations(), appraisal.dataset, appraisal.policy, budget=Decimal("20000")
        )
        self.assertLessEqual(portfolio.total_cost, Decimal("20000"))

    def test_shared_capacity_blocks_second_scenario(self):
        builder = FixtureBuilder()
        builder.complete_scenario("S-A")
        builder.dependencies[-1]["shared_group"] = "SH-01"
        builder.dependencies[-1]["capacity_units"] = "8"
        builder.complete_scenario("S-B")
        builder.dependencies[-1]["shared_group"] = "SH-01"
        builder.dependencies[-1]["capacity_units"] = "8"
        with tempfile.TemporaryDirectory() as tmp:
            builder.write(Path(tmp))
            policy = replace(load_policy(), capacity_by_group={"SH-01": 10})
            appraisal = build_appraisal(tmp, registry_root=None, policy=policy)
        portfolio = select_portfolio(
            appraisal.ordered_valuations(), appraisal.dataset, policy, budget=Decimal("100000")
        )
        # 同一共享组容量 10，每个占 8，最多选一个。
        self.assertEqual(len(portfolio.selected), 1)
        self.assertEqual(portfolio.group_usage.get("SH-01"), 8)

    def test_deterministic_regardless_of_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            appraisal = _appraise(self._base_builder(), tmp)
        forward = select_portfolio(appraisal.ordered_valuations(), appraisal.dataset, appraisal.policy)
        backward = select_portfolio(
            list(reversed(appraisal.ordered_valuations())), appraisal.dataset, appraisal.policy
        )
        self.assertEqual(forward.selected_ids, backward.selected_ids)
        self.assertEqual(forward.total_value, backward.total_value)

    def test_budget_sensitivity_marks_added_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            appraisal = _appraise(self._base_builder(), tmp)
        points = appraisal.sensitivity.points
        self.assertTrue(points)
        budgets = [point.budget for point in points]
        self.assertEqual(budgets, sorted(budgets))
        for point in points:
            overlap = set(point.added) & set(point.removed)
            self.assertFalse(overlap)

    def test_over_released_funding_does_not_create_budget(self):
        builder = FixtureBuilder().complete_scenario("S-A")
        builder.funding[-1].update({"amount": "10000", "released_amount": "15000", "committee_decision": "approved", "result_state": "in-progress"})
        builder.stage("S-A", amount=5000, release_condition="pilot-target", committee_decision="held")
        with tempfile.TemporaryDirectory() as tmp:
            appraisal = _appraise(builder, tmp)
        valuation = appraisal.valuations["S-A"]
        # 首阶段已超额释放，启动所需未释放额度来自第二阶段 5000，而非 0 或负数。
        self.assertEqual(valuation.startup_funding, Decimal("5000"))

    def test_excluded_reason_mentions_budget_when_nothing_else(self):
        builder = FixtureBuilder()
        for sid, hours in (("S-A", 30), ("S-B", 20)):
            builder.complete_scenario(sid, monthly_volume=1000, baseline_hours=hours)
            builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": str(hours * 2)})
        with tempfile.TemporaryDirectory() as tmp:
            appraisal = _appraise(builder, tmp)
        portfolio = select_portfolio(
            appraisal.ordered_valuations(), appraisal.dataset, appraisal.policy, budget=Decimal("21000")
        )
        self.assertEqual(len(portfolio.selected), 1)
        reasons = {val.scenario_id: reason for val, reason in portfolio.excluded}
        unselected = (set(reasons) & {"S-A", "S-B"}) - set(portfolio.selected_ids)
        self.assertEqual(len(unselected), 1)
        self.assertIn("预算", reasons[next(iter(unselected))])


if __name__ == "__main__":
    unittest.main()
