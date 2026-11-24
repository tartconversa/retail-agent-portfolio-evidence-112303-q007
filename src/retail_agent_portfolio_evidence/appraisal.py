"""应用服务：加载资料、评估全部场景、构建组合并产出可持久化报告。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .evidence import EvidenceProfile, assess_evidence
from .loader import DataIssue, load_dataset
from .models import Dataset, Scenario
from .policy import Policy, load_policy
from .portfolio import Portfolio, SensitivityReport, budget_sensitivity, select_portfolio
from .registry import Registry, ScenarioRecord
from .review import ReviewOutcome, review_result
from .valuation import Valuation, value_scenario


@dataclass
class Appraisal:
    policy: Policy
    dataset: Dataset
    issues: list[DataIssue]
    evidence: dict[str, EvidenceProfile]
    valuations: dict[str, Valuation]
    portfolio: Portfolio
    sensitivity: SensitivityReport
    reviews: dict[str, ReviewOutcome]
    registry_records: dict[str, ScenarioRecord]
    data_dir: Path
    reduction_key: str

    def ordered_valuations(self) -> list[Valuation]:
        return [self.valuations[sid] for sid in self.dataset.scenario_order]

    def valuation_rows(self) -> list[Valuation]:
        return sorted(
            self.ordered_valuations(),
            key=lambda v: (-v.risk_adjusted_annual_value, v.scenario_id),
        )


def build_appraisal(
    data_dir: str | Path,
    *,
    policy: Policy | None = None,
    policy_path: str | Path | None = None,
    registry_root: str | Path | None = None,
    budget: Decimal | None = None,
    reduction_key: str = "base",
) -> Appraisal:
    active_policy = policy or load_policy(policy_path)
    dataset, issues = load_dataset(data_dir)
    records: dict[str, ScenarioRecord] = {}
    if registry_root is not None:
        registry = Registry(registry_root)
        if registry.exists():
            records = registry.load()

    evidence: dict[str, EvidenceProfile] = {}
    valuations: dict[str, Valuation] = {}
    reviews: dict[str, ReviewOutcome] = {}
    for scenario in dataset.iter_scenarios():
        profile = assess_evidence(scenario, dataset, active_policy)
        record = records.get(scenario.scenario_id)
        valuations[scenario.scenario_id] = value_scenario(
            scenario,
            dataset,
            active_policy,
            profile,
            acceptance_defined=record is not None and record.acceptance is not None,
            reduction_key=reduction_key,
        )
        evidence[scenario.scenario_id] = profile
        reviews[scenario.scenario_id] = review_result(scenario.scenario_id, record, dataset, profile)

    ordered = [valuations[sid] for sid in dataset.scenario_order]
    portfolio = select_portfolio(ordered, dataset, active_policy, budget=budget)
    sensitivity = budget_sensitivity(ordered, dataset, active_policy)
    return Appraisal(
        policy=active_policy,
        dataset=dataset,
        issues=issues,
        evidence=evidence,
        valuations=valuations,
        portfolio=portfolio,
        sensitivity=sensitivity,
        reviews=reviews,
        registry_records=records,
        data_dir=Path(data_dir),
        reduction_key=reduction_key,
    )


def funding_summary(dataset: Dataset, policy: Policy, *, limit: int = 12) -> dict[str, Any]:
    """资金总览：授权、已释放、超额、未释放。"""
    stages = dataset.funding
    authorised = sum((stage.amount for stage in stages), Decimal("0"))
    released_raw = sum((stage.released_amount for stage in stages), Decimal("0"))
    released_capped = sum((stage.released_capped for stage in stages), Decimal("0"))
    over = sum((stage.over_released for stage in stages), Decimal("0"))
    outstanding = sum((stage.outstanding for stage in stages), Decimal("0"))
    blockers = [
        (stage.stage_id, stage.scenario_id, stage.over_released)
        for stage in stages
        if stage.over_released > 0
    ][:limit]
    by_decision: dict[str, Decimal] = {}
    for stage in stages:
        by_decision[stage.committee_decision] = by_decision.get(stage.committee_decision, Decimal("0")) + stage.amount
    return {
        "authorised": authorised,
        "released": released_raw,
        "released_capped": released_capped,
        "over_released": over,
        "outstanding": outstanding,
        "by_committee_decision": by_decision,
        "over_release_examples": blockers,
    }


def to_json(appraisal: Appraisal) -> dict[str, Any]:
    """稳定、可存档的 JSON 视图；金额以字符串保留精度。"""

    def dec(value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None

    valuations = []
    for val in appraisal.valuation_rows():
        valuations.append(
            {
                "scenario_id": val.scenario_id,
                "department": val.department,
                "process": val.process,
                "target_kpi": val.target_kpi,
                "owner_state": val.owner_state,
                "evidence_tier": val.evidence_tier,
                "evidence_confidence": dec(val.evidence_confidence),
                "governance_decision": val.governance_decision,
                "readiness_states": list(val.readiness_states),
                "baseline_monthly_hours": dec(val.baseline_monthly_hours),
                "baseline_monthly_cost": dec(val.baseline_monthly_cost),
                "observed_annual_value": dec(val.observed_annual_value),
                "observed_annual_value_indicative": dec(val.observed_annual_value_indicative),
                "assumption_annual_value": dec(val.assumption_annual_value),
                "risk_adjusted_annual_value": dec(val.risk_adjusted_annual_value),
                "startup_funding": dec(val.startup_funding),
                "total_unreleased_funding": dec(val.total_unreleased_funding),
                "value_cost_ratio": dec(val.value_cost_ratio),
                "payback_years": dec(val.payback_years),
                "observed_monetizable": val.observed_monetizable,
                "assumption_contradicted_by_observation": val.assumption_contradicted_by_observation,
                "entry_eligible": val.entry_eligible,
                "advance_eligible": val.advance_eligible,
                "gates": [
                    {"name": gate.name, "passed": gate.passed, "detail": gate.detail}
                    for gate in val.gates.results
                ],
                "evidence_notes": list(val.evidence_notes),
                "review": {
                    "verdict": appraisal.reviews[val.scenario_id].verdict,
                    "rationale": appraisal.reviews[val.scenario_id].rationale,
                },
            }
        )

    portfolio = appraisal.portfolio
    funding = funding_summary(appraisal.dataset, appraisal.policy)
    return {
        "policy_version": appraisal.policy.policy_version,
        "currency": appraisal.policy.currency,
        "reduction_key": appraisal.reduction_key,
        "budget": dec(portfolio.budget),
        "capacity": {group: cap for group, cap in sorted(portfolio.capacities.items())},
        "selected": portfolio.selected_ids,
        "total_risk_adjusted_value": dec(portfolio.total_value),
        "total_startup_cost": dec(portfolio.total_cost),
        "group_usage": dict(sorted(portfolio.group_usage.items())),
        "binding_groups": portfolio.binding_groups,
        "budget_utilisation": dec(portfolio.total_cost / portfolio.budget) if portfolio.budget else None,
        "search_complete": portfolio.search_complete,
        "excluded": [
            {"scenario_id": val.scenario_id, "reason": reason} for val, reason in portfolio.excluded
        ],
        "valuations": valuations,
        "funding": {
            "authorised": dec(funding["authorised"]),
            "released": dec(funding["released"]),
            "released_capped": dec(funding["released_capped"]),
            "over_released": dec(funding["over_released"]),
            "outstanding": dec(funding["outstanding"]),
            "by_committee_decision": {k: dec(v) for k, v in sorted(funding["by_committee_decision"].items())},
            "over_release_examples": [
                {"stage_id": sid, "scenario_id": scn, "over_released": dec(amount)}
                for sid, scn, amount in funding["over_release_examples"]
            ],
        },
        "data_issues": [
            {"code": issue.code, "scenario_id": issue.scenario_id, "detail": issue.detail}
            for issue in appraisal.issues
        ],
        "sensitivity": {
            "budget": [
                {
                    "budget": dec(point.budget),
                    "selected": list(point.selected_ids),
                    "total_value": dec(point.total_value),
                    "total_cost": dec(point.total_cost),
                    "added": list(point.added),
                    "removed": list(point.removed),
                }
                for point in appraisal.sensitivity.points
            ],
            "shared_capacity": [
                {
                    "multiplier": str(mult),
                    "selected": list(point.selected_ids),
                    "total_value": dec(point.total_value),
                    "total_cost": dec(point.total_cost),
                    "added": list(point.added),
                    "removed": list(point.removed),
                }
                for mult, point in zip(("0.75", "1.0", "1.25"), appraisal.sensitivity.capacity_points)
            ],
        },
        "reviews": [
            {
                "scenario_id": outcome.scenario_id,
                "verdict": outcome.verdict,
                "rationale": outcome.rationale,
            }
            for outcome in (appraisal.reviews[sid] for sid in appraisal.dataset.scenario_order)
        ],
    }
