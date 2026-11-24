"""端到端闭环：挑选、登记验收、释放、回看、未达标暂停、保留已用资金、停止。

验证需求中的运营闭环，而不是单点函数：
- 试点未达约定指标时可暂停未释放资金，但已释放与已形成合同金额保留；
- 回看使用登记时的原始验收口径；
- 停止后场景不再参与后续投资决策。
"""

from decimal import Decimal
import io
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from fixtures import FixtureBuilder

from retail_agent_portfolio_evidence.appraisal import build_appraisal
from retail_agent_portfolio_evidence.cli import main
from retail_agent_portfolio_evidence.registry import Registry


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.registry = self.root / "registry"
        builder = FixtureBuilder()
        builder.complete_scenario("S-1", monthly_volume=1000, baseline_hours=40)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "150"})  # 0.05 小时/件
        builder.write(self.data)

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _register_and_release(self, threshold: str) -> None:
        code, _out, err = self._run(
            "register", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--metric", "unit-hours", "--direction", "at-most", "--threshold", threshold,
            "--unit", "hours/case", "--measured-from", "2026-Q1",
            "--actor", "业务负责人", "--rationale", "约定单位工时阈值",
        )
        self.assertEqual(code, 0, err)
        code, _out, err = self._run(
            "decide", "release", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "释放试点阶段", "--stage", "pilot",
        )
        self.assertEqual(code, 0, err)

    def test_pause_after_missed_target_preserves_released(self):
        # 阈值 0.04，观察 0.05 → 未达标。
        self._register_and_release("0.04")

        code, out, err = self._run("review", "--data", str(self.data), "--registry", str(self.registry))
        self.assertEqual(code, 0, err)
        self.assertIn("未达标", out)

        # 先释放了 20000，随后暂停只能针对未释放余额；本用例首阶段已全部释放。
        code, _out, err = self._run(
            "decide", "pause", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "未达约定指标，暂停未释放资金",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("已释放与已形成合同金额须保留", err)

        # 再加一个后续阶段后，暂停只能拿到后续阶段的未释放额度。
        builder = FixtureBuilder()
        builder.complete_scenario("S-1", monthly_volume=1000, baseline_hours=40)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "150"})
        builder.stage("S-1", amount=15000, release_condition="pilot-target", committee_decision="held")
        builder.write(self.data)
        code, _out, err = self._run(
            "decide", "pause", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "暂停第二阶段未释放资金",
        )
        self.assertEqual(code, 0, err)
        records = Registry(self.registry).load()
        self.assertEqual(records["S-1"].funds_paused_total, Decimal("15000"))

    def test_met_target_allows_advance(self):
        self._register_and_release("0.06")
        code, out, err = self._run("review", "--data", str(self.data), "--registry", str(self.registry))
        self.assertEqual(code, 0, err)
        self.assertIn("达标", out)
        code, _out, err = self._run(
            "decide", "advance", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "达到约定指标", "--stage", "scale",
        )
        self.assertEqual(code, 0, err)
        records = Registry(self.registry).load()
        self.assertEqual(records["S-1"].current_stage, "scale")

    def test_advance_without_acceptance_is_rejected(self):
        code, _out, err = self._run(
            "decide", "advance", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "未定义验收就想推进",
        )
        self.assertEqual(code, 2)
        self.assertIn("验收口径", err)

    def test_stop_removes_from_active_and_keeps_history(self):
        self._register_and_release("0.06")
        code, _out, err = self._run(
            "decide", "stop", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "业务目标取消",
        )
        self.assertEqual(code, 0, err)
        records = Registry(self.registry).load()
        self.assertFalse(records["S-1"].active)
        # 历史仍在，可回溯原始验收口径。
        self.assertEqual(len(records["S-1"].decision_log), 3)
        self.assertEqual(records["S-1"].acceptance.threshold, Decimal("0.06"))

    def test_release_cannot_exceed_stage_outstanding(self):
        self._register_and_release("0.06")
        code, _out, err = self._run(
            "decide", "release", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "重复释放", "--amount", "999999",
        )
        self.assertEqual(code, 2)
        self.assertIn("超过当前阶段未释放余额", err)

    def test_release_requires_acceptance(self):
        code, _out, err = self._run(
            "decide", "release", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "未登记验收就想释放",
        )
        self.assertEqual(code, 2)
        self.assertIn("验收口径", err)

    def test_release_blocked_when_latest_governance_revise(self):
        builder = FixtureBuilder()
        builder.complete_scenario("S-1", monthly_volume=1000, baseline_hours=40)
        builder.pilots[-1].update({"observed_volume": "3000", "observed_hours": "150"})
        builder.review("S-1", decision="revise", reviewed_at="2026-11-20")
        builder.write(self.data)
        self._run(
            "register", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--metric", "unit-hours", "--direction", "at-most", "--threshold", "0.06",
            "--unit", "hours/case", "--measured-from", "2026-Q1",
            "--actor", "业务负责人", "--rationale", "约定",
        )
        code, _out, err = self._run(
            "decide", "release", "S-1", "--data", str(self.data), "--registry", str(self.registry),
            "--actor", "投资委员会", "--rationale", "治理未通过仍想释放",
        )
        self.assertEqual(code, 2)
        self.assertIn("数据治理结论", err)

    def test_json_output_is_stable_and_complete(self):
        code, out, err = self._run("evaluate", "--data", str(self.data), "--format", "json")
        self.assertEqual(code, 0, err)
        import json
        payload = json.loads(out)
        self.assertEqual(payload["selected"], ["S-1"])
        self.assertIn("sensitivity", payload)
        self.assertIn("funding", payload)


if __name__ == "__main__":
    unittest.main()
