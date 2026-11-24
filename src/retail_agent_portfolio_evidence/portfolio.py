"""组合选择：在预算与共享实施容量下最大化风险调整后年度价值。

模型：
- 每个场景的启动成本 = 最早仍有未释放余额的资金阶段金额（后续阶段不预先占用）。
- 每个场景占用其依赖所在共享组的能力单位；同一共享组的总占用不得超过政策容量。
- 目标为风险调整后年度价值之和，不做场景间的协同加成假设。

对进入设计阶段的候选集合使用确定性的精确分支定界枚举；结果与输入顺序无关。
未通过入口闸门的场景不进入组合，但仍会出现在管线与"若条件改变"说明中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from .models import Dataset
from .policy import Policy
from .valuation import Valuation

NODE_LIMIT = 2_000_000


@dataclass(frozen=True)
class Candidate:
    valuation: Valuation
    cost: Decimal
    capacity: Mapping[str, int]


@dataclass
class Portfolio:
    budget: Decimal
    capacities: Mapping[str, int]
    selected: list[Valuation]
    total_value: Decimal
    total_cost: Decimal
    group_usage: dict[str, int]
    binding_groups: list[str]
    excluded: list[tuple[Valuation, str]]
    search_complete: bool
    nodes_searched: int

    @property
    def selected_ids(self) -> list[str]:
        return [item.scenario_id for item in self.selected]

    def usage_ratio(self, group: str) -> Decimal:
        capacity = self.capacities.get(group, 0)
        if capacity <= 0:
            return Decimal("0")
        return Decimal(self.group_usage.get(group, 0)) / Decimal(capacity)


def capacity_for(scenario_id: str, dataset: Dataset, policy: Policy) -> dict[str, int]:
    usage: dict[str, int] = {}
    for dep in dataset.dependencies_for(scenario_id):
        usage[dep.shared_group] = usage.get(dep.shared_group, 0) + dep.capacity_units
    return usage


def _fits(usage: Mapping[str, int], caps_left: Mapping[str, int]) -> bool:
    return all(caps_left.get(group, 0) >= units for group, units in usage.items())


def _subtract(caps: Mapping[str, int], usage: Mapping[str, int]) -> dict[str, int]:
    return {group: caps[group] - usage.get(group, 0) for group in caps}


def _add(caps: Mapping[str, int], usage: Mapping[str, int]) -> dict[str, int]:
    return {group: caps[group] + usage.get(group, 0) for group in caps}


def _select_exact(candidates: Sequence[Candidate], budget: Decimal, capacities: Mapping[str, int]) -> tuple[list[Candidate], bool, int]:
    ordered = sorted(
        candidates,
        key=lambda cand: (
            -(cand.valuation.risk_adjusted_annual_value / cand.cost) if cand.cost > 0 else Decimal("-1e30"),
            cand.valuation.scenario_id,
        ),
    )
    n = len(ordered)
    suffix_value = [Decimal("0")] * (n + 1)
    for idx in range(n - 1, -1, -1):
        suffix_value[idx] = suffix_value[idx + 1] + ordered[idx].valuation.risk_adjusted_annual_value

    best_value = Decimal("-1")
    best_cost = Decimal("0")
    best_key: tuple[str, ...] = ()
    best_choice: list[Candidate] = []
    nodes = 0
    complete = True

    def key_for(choice: Iterable[Candidate]) -> tuple[str, ...]:
        return tuple(sorted(cand.valuation.scenario_id for cand in choice))

    def visit(index: int, budget_left: Decimal, caps_left: Mapping[str, int], value: Decimal, cost: Decimal, choice: list[Candidate]) -> None:
        nonlocal best_value, best_cost, best_key, best_choice, nodes, complete
        nodes += 1
        if nodes > NODE_LIMIT:
            complete = False
            return
        if value + suffix_value[index] < best_value:
            return
        if index == n:
            key = key_for(choice)
            if (
                value > best_value
                or (value == best_value and cost < best_cost)
                or (value == best_value and cost == best_cost and key < best_key)
            ):
                best_value = value
                best_cost = cost
                best_key = key
                best_choice = list(choice)
            return

        cand = ordered[index]
        if cand.cost <= budget_left and _fits(cand.capacity, caps_left):
            choice.append(cand)
            visit(
                index + 1,
                budget_left - cand.cost,
                _subtract(caps_left, cand.capacity),
                value + cand.valuation.risk_adjusted_annual_value,
                cost + cand.cost,
                choice,
            )
            choice.pop()
        visit(index + 1, budget_left, caps_left, value, cost, choice)

    visit(0, budget, dict(capacities), Decimal("0"), Decimal("0"), [])
    return best_choice, complete, nodes


def build_candidates(valuations: Iterable[Valuation], dataset: Dataset, policy: Policy, *, eligible_only: bool = True) -> tuple[list[Candidate], list[tuple[Valuation, str]]]:
    candidates: list[Candidate] = []
    excluded: list[tuple[Valuation, str]] = []
    for valuation in valuations:
        if eligible_only and not valuation.entry_eligible:
            failed = "；".join(f"{gate.label}未通过（{gate.detail}）" for gate in valuation.gates.failed)
            excluded.append((valuation, f"入口闸门未通过：{failed}"))
            continue
        if valuation.startup_funding <= 0:
            excluded.append((valuation, "无未释放启动资金，无需新拨款"))
            continue
        if valuation.risk_adjusted_annual_value <= 0:
            excluded.append((valuation, "风险调整后价值为 0，排序无意义"))
            continue
        candidates.append(
            Candidate(
                valuation=valuation,
                cost=valuation.startup_funding,
                capacity=capacity_for(valuation.scenario_id, dataset, policy),
            )
        )
    return candidates, excluded


def _explain_unselected(
    portfolio_ids: set[str],
    candidates: Sequence[Candidate],
    budget: Decimal,
    capacities: Mapping[str, int],
) -> list[tuple[Valuation, str]]:
    """对未入选的合格候选给出预算或容量层面的原因。"""
    explanations: list[tuple[Valuation, str]] = []
    remaining_budget = budget - sum(
        cand.cost for cand in candidates if cand.valuation.scenario_id in portfolio_ids
    )
    used: dict[str, int] = {}
    for cand in candidates:
        if cand.valuation.scenario_id in portfolio_ids:
            for group, units in cand.capacity.items():
                used[group] = used.get(group, 0) + units

    for cand in candidates:
        if cand.valuation.scenario_id in portfolio_ids:
            continue
        reasons: list[str] = []
        if cand.cost > remaining_budget:
            reasons.append(f"启动资金 {cand.cost} 超出剩余预算 {remaining_budget}")
        for group, units in sorted(cand.capacity.items()):
            free = capacities.get(group, 0) - used.get(group, 0)
            if units > free:
                reasons.append(f"共享组 {group} 剩余容量 {free} < 需要 {units}")
        if not reasons:
            reasons.append("与更高价值的组合相比，加入它会挤占预算或共享容量")
        explanations.append((cand.valuation, "；".join(dict.fromkeys(reasons))))
    return explanations


def select_portfolio(
    valuations: Sequence[Valuation],
    dataset: Dataset,
    policy: Policy,
    *,
    budget: Decimal | None = None,
) -> Portfolio:
    effective_budget = budget if budget is not None else policy.default_budget_cny
    candidates, gate_excluded = build_candidates(valuations, dataset, policy)
    chosen, complete, nodes = _select_exact(candidates, effective_budget, policy.capacity_by_group)

    chosen_ids = {cand.valuation.scenario_id for cand in chosen}
    group_usage: dict[str, int] = {}
    for cand in chosen:
        for group, units in cand.capacity.items():
            group_usage[group] = group_usage.get(group, 0) + units
    binding = sorted(
        group for group, units in group_usage.items() if units >= policy.group_capacity(group)
    )
    excluded = gate_excluded + _explain_unselected(chosen_ids, candidates, effective_budget, policy.capacity_by_group)
    return Portfolio(
        budget=effective_budget,
        capacities=dict(policy.capacity_by_group),
        selected=sorted((cand.valuation for cand in chosen), key=lambda v: (-v.risk_adjusted_annual_value, v.scenario_id)),
        total_value=sum((cand.valuation.risk_adjusted_annual_value for cand in chosen), Decimal("0")),
        total_cost=sum((cand.cost for cand in chosen), Decimal("0")),
        group_usage=group_usage,
        binding_groups=binding,
        excluded=excluded,
        search_complete=complete,
        nodes_searched=nodes,
    )


@dataclass(frozen=True)
class SensitivityPoint:
    budget: Decimal
    selected_ids: tuple[str, ...]
    total_value: Decimal
    total_cost: Decimal
    added: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def utilisation(self) -> Decimal:
        return (self.total_cost / self.budget) if self.budget > 0 else Decimal("0")


@dataclass
class SensitivityReport:
    points: list[SensitivityPoint]
    capacity_points: list[SensitivityPoint] = field(default_factory=list)


def budget_sensitivity(valuations: Sequence[Valuation], dataset: Dataset, policy: Policy) -> SensitivityReport:
    baseline = select_portfolio(valuations, dataset, policy)
    baseline_ids = set(baseline.selected_ids)
    points: list[SensitivityPoint] = []
    for multiplier in sorted(policy.budget_sensitivity_multipliers):
        budget = (policy.default_budget_cny * multiplier).quantize(Decimal("0.01"))
        portfolio = select_portfolio(valuations, dataset, policy, budget=budget)
        current = set(portfolio.selected_ids)
        points.append(
            SensitivityPoint(
                budget=budget,
                selected_ids=tuple(portfolio.selected_ids),
                total_value=portfolio.total_value,
                total_cost=portfolio.total_cost,
                added=tuple(sorted(current - baseline_ids)),
                removed=tuple(sorted(baseline_ids - current)),
            )
        )

    capacity_points: list[SensitivityPoint] = []
    for multiplier in (Decimal("0.75"), Decimal("1.0"), Decimal("1.25")):
        scaled = {group: max(0, int(Decimal(cap) * multiplier)) for group, cap in policy.capacity_by_group.items()}
        scaled_policy = _with_capacities(policy, scaled)
        portfolio = select_portfolio(valuations, dataset, scaled_policy)
        current = set(portfolio.selected_ids)
        capacity_points.append(
            SensitivityPoint(
                budget=policy.default_budget_cny,
                selected_ids=tuple(portfolio.selected_ids),
                total_value=portfolio.total_value,
                total_cost=portfolio.total_cost,
                added=tuple(sorted(current - baseline_ids)),
                removed=tuple(sorted(baseline_ids - current)),
            )
        )
    return SensitivityReport(points=points, capacity_points=capacity_points)


def _with_capacities(policy: Policy, capacities: Mapping[str, int]) -> Policy:
    from dataclasses import replace

    return replace(policy, capacity_by_group=dict(capacities))
