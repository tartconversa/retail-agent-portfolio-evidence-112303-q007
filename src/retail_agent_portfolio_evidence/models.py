"""领域模型：候选场景、依赖、治理、资金阶段与试点测量。

所有金额、工时和比例使用 ``Decimal``，避免浮点在预算与拨款核对中产生误差。
缺失状态使用 ``None`` 表示，不折算为零。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    department: str
    process: str
    monthly_volume: Decimal
    baseline_hours: Decimal
    error_rate: Decimal
    target_kpi: str
    owner_state: str

    @property
    def baseline_unit_hours(self) -> Decimal | None:
        """基线口径：月度人工工时 / 月度业务量。"""
        if self.monthly_volume <= 0:
            return None
        return self.baseline_hours / self.monthly_volume


@dataclass(frozen=True)
class Dependency:
    scenario_id: str
    dependency: str
    capacity_units: int
    shared_group: str
    readiness_state: str
    version: str


@dataclass(frozen=True)
class GovernanceReview:
    review_id: str
    scenario_id: str
    data_scope: str
    purpose: str
    risk_level: str
    decision: str
    reviewed_at: date


@dataclass(frozen=True)
class FundingStage:
    stage_id: str
    scenario_id: str
    amount: Decimal
    release_condition: str
    committee_decision: str
    released_amount: Decimal
    result_state: str

    @property
    def released_capped(self) -> Decimal:
        """已释放金额不得超过该阶段授权金额；超额为数据缺陷，不产生额外预算。"""
        return min(self.released_amount, self.amount)

    @property
    def over_released(self) -> Decimal:
        return max(self.released_amount - self.amount, Decimal("0"))

    @property
    def outstanding(self) -> Decimal:
        return max(self.amount - self.released_amount, Decimal("0"))


@dataclass(frozen=True)
class PilotResult:
    result_id: str
    scenario_id: str
    period: str
    observed_volume: Decimal
    observed_hours: Decimal
    outcome_value: Decimal
    measurement_state: str
    source_version: str


@dataclass
class Dataset:
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    scenario_order: list[str] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    governance: list[GovernanceReview] = field(default_factory=list)
    funding: list[FundingStage] = field(default_factory=list)
    pilots: list[PilotResult] = field(default_factory=list)

    def add_scenario(self, scenario: Scenario) -> None:
        if scenario.scenario_id not in self.scenarios:
            self.scenario_order.append(scenario.scenario_id)
        self.scenarios[scenario.scenario_id] = scenario

    def dependencies_for(self, scenario_id: str) -> list[Dependency]:
        return [row for row in self.dependencies if row.scenario_id == scenario_id]

    def governance_for(self, scenario_id: str) -> list[GovernanceReview]:
        return [row for row in self.governance if row.scenario_id == scenario_id]

    def funding_for(self, scenario_id: str) -> list[FundingStage]:
        return [row for row in self.funding if row.scenario_id == scenario_id]

    def pilots_for(self, scenario_id: str) -> list[PilotResult]:
        return [row for row in self.pilots if row.scenario_id == scenario_id]

    def iter_scenarios(self) -> Iterable[Scenario]:
        for scenario_id in self.scenario_order:
            yield self.scenarios[scenario_id]
