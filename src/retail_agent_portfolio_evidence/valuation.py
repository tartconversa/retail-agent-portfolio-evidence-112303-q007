"""价值评估：把基线、证据、治理、业务承诺和就绪度分开计价。

三类价值始终分开呈现：
1. ``observed``：只在试点口径可复核、可外推时给出，且取最保守的一条测量；
2. ``assumed``：按政策中的折减率假设推算，未经试点证实；
3. ``risk_adjusted``：假设价值再乘以证据置信度、治理系数、业务承诺系数与就绪系数。

排序与组合选择使用 ``risk_adjusted``，但报告同时展示已观察价值为零或为负的事实，
避免把假设收益当作已经实现的收益。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .evidence import EvidenceProfile, TIER_LABELS
from .gating import ScenarioGates, evaluate_gates, funding_request, latest_governance
from .models import Dataset, Scenario
from .policy import Policy

MONEY = Decimal("0.01")
RATIO = Decimal("0.0001")


def _q(value: Decimal, unit: Decimal = MONEY) -> Decimal:
    return value.quantize(unit, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Valuation:
    scenario_id: str
    department: str
    process: str
    owner_state: str
    target_kpi: str
    evidence_tier: str
    evidence_confidence: Decimal
    governance_decision: str
    governance_factor: Decimal
    owner_factor: Decimal
    readiness_factor: Decimal
    readiness_states: tuple[str, ...]
    baseline_monthly_hours: Decimal
    baseline_monthly_cost: Decimal
    assumed_reduction: Decimal
    observed_annual_value: Decimal
    observed_annual_value_indicative: Decimal
    assumption_annual_value: Decimal
    risk_adjusted_annual_value: Decimal
    startup_funding: Decimal
    total_unreleased_funding: Decimal
    value_cost_ratio: Decimal | None
    payback_years: Decimal | None
    gates: ScenarioGates
    observed_monetizable: bool
    assumption_contradicted_by_observation: bool
    evidence_notes: tuple[str, ...]

    @property
    def entry_eligible(self) -> bool:
        """进入设计/数据治理阶段所需闸门（验收口径在推进到试点释放时再要求）。"""
        entry = {"owner", "governance", "readiness", "funding", "baseline"}
        return all(result.passed for result in self.gates.results if result.name in entry)

    @property
    def advance_eligible(self) -> bool:
        """释放试点阶段资金还要求验收口径已定义。"""
        return self.gates.passed

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.evidence_tier, self.evidence_tier)


def _observed_anchor(evidence: EvidenceProfile) -> Decimal | None:
    if not evidence.observed_monetizable:
        return None
    return evidence.observed_delta_conservative


def value_scenario(
    scenario: Scenario,
    dataset: Dataset,
    policy: Policy,
    evidence: EvidenceProfile,
    *,
    acceptance_defined: bool = False,
    reduction_key: str = "base",
) -> Valuation:
    sid = scenario.scenario_id
    assumed = policy.assumed_reduction_rate.get(reduction_key)
    if assumed is None:
        raise ValueError(f"政策中没有折减率档位: {reduction_key}")

    review = latest_governance(dataset.governance_for(sid))
    gov_decision = review.decision if review else "none"
    gov_factor = policy.governance_factor.get(gov_decision, Decimal("0"))
    owner_factor = policy.owner_factor.get(scenario.owner_state, Decimal("0"))
    deps = dataset.dependencies_for(sid)
    states = tuple(sorted({dep.readiness_state for dep in deps})) or ("unknown",)
    readiness_factor = min(
        (policy.readiness_factor.get(state, Decimal("0")) for state in states), default=Decimal("0")
    )

    monthly_cost = policy.scenario_hour_cost(scenario.baseline_hours)
    assumption_annual = monthly_cost * policy.annualization_months * assumed
    factor = max(
        evidence.effective_confidence * gov_factor * owner_factor * readiness_factor,
        Decimal("0"),
    )
    risk_adjusted = assumption_annual * factor

    observed_value = (
        evidence.observed_annual_value_conservative
        if evidence.observed_monetizable and evidence.observed_annual_value_conservative is not None
        else Decimal("0")
    )
    indicative = evidence.observed_annual_value_conservative if evidence.observed_annual_value_conservative is not None else Decimal("0")
    anchor = _observed_anchor(evidence)
    contradicted = evidence.assumption_status == "contradicted"
    if contradicted:
        risk_adjusted *= Decimal("0.5")
    risk_adjusted = _q(risk_adjusted)
    startup, total = funding_request(sid, dataset)
    ratio = None
    payback = None
    if startup > 0:
        ratio = _q(risk_adjusted / startup, RATIO)
        payback = _q(startup / risk_adjusted, RATIO) if risk_adjusted > 0 else None

    notes = list(evidence.notes)
    if contradicted:
        notes.append(
            "已观察到的单位工时方向与假设相反；假设收益未被证据支持，风险调整值再打 50% 折扣。"
        )
    if evidence.observed_delta_conservative is not None and not evidence.observed_monetizable:
        notes.append("试点覆盖范围或测量状态不足以把观察结果外推为全量年度节省。")

    gates = evaluate_gates(scenario, dataset, policy, acceptance_defined=acceptance_defined)
    return Valuation(
        scenario_id=sid,
        department=scenario.department,
        process=scenario.process,
        owner_state=scenario.owner_state,
        target_kpi=scenario.target_kpi,
        evidence_tier=evidence.tier,
        evidence_confidence=evidence.effective_confidence,
        governance_decision=gov_decision,
        governance_factor=gov_factor,
        owner_factor=owner_factor,
        readiness_factor=readiness_factor,
        readiness_states=states,
        baseline_monthly_hours=scenario.baseline_hours,
        baseline_monthly_cost=_q(monthly_cost),
        assumed_reduction=assumed,
        observed_annual_value=_q(observed_value),
        observed_annual_value_indicative=_q(indicative),
        assumption_annual_value=_q(assumption_annual),
        risk_adjusted_annual_value=_q(risk_adjusted),
        startup_funding=_q(startup),
        total_unreleased_funding=_q(total),
        value_cost_ratio=ratio,
        payback_years=payback,
        gates=gates,
        observed_monetizable=evidence.observed_monetizable,
        assumption_contradicted_by_observation=contradicted,
        evidence_notes=tuple(notes),
    )
