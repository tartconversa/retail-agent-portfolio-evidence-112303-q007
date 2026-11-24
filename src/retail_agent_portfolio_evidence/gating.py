"""闸门判定：把"技术验证通过"与"业务方承诺采用"分开。

每个闸门给出通过/未通过及原因，而不是压缩成一个总分。投资决策可以覆盖闸门，
但覆盖必须显式记录在决策日志中。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Dataset, FundingStage, GovernanceReview, Scenario
from .policy import Policy

GATE_LABELS = {
    "owner": "业务负责人承诺",
    "governance": "数据治理结论",
    "readiness": "依赖系统就绪",
    "funding": "存在未释放资金",
    "baseline": "基线可计算",
    "acceptance": "验收口径已定义",
}


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str

    @property
    def label(self) -> str:
        return GATE_LABELS.get(self.name, self.name)


@dataclass(frozen=True)
class ScenarioGates:
    scenario_id: str
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failed(self) -> tuple[GateResult, ...]:
        return tuple(result for result in self.results if not result.passed)


def latest_governance(reviews: list[GovernanceReview]) -> GovernanceReview | None:
    if not reviews:
        return None
    return max(reviews, key=lambda review: (review.reviewed_at, review.review_id))


def next_outstanding_stage(scenario_id: str, dataset: Dataset) -> FundingStage | None:
    """返回最早一个仍有未释放余额的资金阶段。"""
    ordered = sorted(dataset.funding_for(scenario_id), key=_stage_order)
    return next((stage for stage in ordered if stage.outstanding > 0), None)


def funding_request(scenario_id: str, dataset: Dataset) -> tuple[Decimal, Decimal]:
    """返回 (启动所需未释放金额, 全部阶段未释放金额)。

    启动金额取最早一个仍有未释放余额的阶段；后续阶段依赖试点结果，不预先占用预算。
    已超额释放的部分不会再占用预算。
    """
    stages = dataset.funding_for(scenario_id)
    ordered = sorted(stages, key=_stage_order)
    outstanding = [stage.outstanding for stage in ordered]
    first = next((amount for amount in outstanding if amount > 0), Decimal("0"))
    return first, sum(outstanding, Decimal("0"))


def _stage_order(stage: FundingStage) -> tuple[int, str]:
    priority = {"owner-commitment": 0, "data-review": 1, "pilot-target": 2}
    return priority.get(stage.release_condition, 9), stage.stage_id


def evaluate_gates(
    scenario: Scenario,
    dataset: Dataset,
    policy: Policy,
    *,
    acceptance_defined: bool = False,
) -> ScenarioGates:
    sid = scenario.scenario_id
    rules = policy.selection
    results: list[GateResult] = []

    owner_ok = scenario.owner_state == "sponsor-confirmed" or not rules.require_owner_confirmed
    results.append(
        GateResult(
            "owner",
            owner_ok,
            f"owner_state={scenario.owner_state}"
            + ("" if scenario.owner_state == "sponsor-confirmed" else "；技术可用不等于业务方承诺采用"),
        )
    )

    review = latest_governance(dataset.governance_for(sid))
    if review is None:
        gov_ok = not rules.require_governance_not_revise
        gov_detail = "没有数据治理审查记录"
    else:
        gov_ok = review.decision != "revise" or not rules.require_governance_not_revise
        gov_detail = f"最新结论={review.decision}（{review.reviewed_at.isoformat()}，风险={review.risk_level}）"
    results.append(GateResult("governance", gov_ok, gov_detail))

    deps = dataset.dependencies_for(sid)
    blocked = [dep for dep in deps if dep.readiness_state == "blocked"]
    if not deps:
        ready_ok = not rules.require_no_blocked_dependency
        ready_detail = "未登记依赖系统"
    else:
        ready_ok = not blocked or not rules.require_no_blocked_dependency
        states = sorted({dep.readiness_state for dep in deps})
        ready_detail = f"依赖就绪状态={','.join(states)}" + (
            f"；阻塞项={','.join(dep.dependency for dep in blocked)}" if blocked else ""
        )
    results.append(GateResult("readiness", ready_ok, ready_detail))

    first_ask, total_ask = funding_request(sid, dataset)
    funding_ok = first_ask > 0 or not rules.require_unreleased_funding
    results.append(
        GateResult(
            "funding",
            funding_ok,
            f"启动未释放={first_ask}，全部未释放={total_ask}",
        )
    )

    baseline_ok = scenario.monthly_volume > 0 and scenario.baseline_hours > 0
    results.append(
        GateResult(
            "baseline",
            baseline_ok,
            f"月业务量={scenario.monthly_volume}，月基线工时={scenario.baseline_hours}"
            if baseline_ok
            else "月业务量或基线工时为 0，无法形成可归因基线",
        )
    )

    results.append(
        GateResult(
            "acceptance",
            acceptance_defined,
            "已记录验收指标与阈值" if acceptance_defined else "尚未记录可量化的验收指标与阈值",
        )
    )
    return ScenarioGates(scenario_id=sid, results=tuple(results))
