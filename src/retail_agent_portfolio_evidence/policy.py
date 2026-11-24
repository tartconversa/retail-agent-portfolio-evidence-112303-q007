"""政策参数加载与校验。

政策参数（小时成本、折减率、证据置信度、容量上限、筛选门槛）全部显式配置，
不写死在计算代码里，便于投资委员会调整假设并复核敏感性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.json"


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SelectionRules:
    require_owner_confirmed: bool = True
    require_governance_not_revise: bool = True
    require_no_blocked_dependency: bool = True
    require_unreleased_funding: bool = True


@dataclass(frozen=True)
class Policy:
    policy_version: str
    currency: str
    loaded_hourly_cost_cny: Decimal
    annualization_months: int
    default_period_months: int
    default_budget_cny: Decimal
    default_group_capacity: int
    capacity_by_group: Mapping[str, int]
    assumed_reduction_rate: Mapping[str, Decimal]
    evidence_confidence: Mapping[str, Decimal]
    governance_factor: Mapping[str, Decimal]
    owner_factor: Mapping[str, Decimal]
    readiness_factor: Mapping[str, Decimal]
    budget_sensitivity_multipliers: tuple[Decimal, ...]
    selection: SelectionRules

    def group_capacity(self, group: str) -> int:
        return int(self.capacity_by_group.get(group, self.default_group_capacity))

    def scenario_hour_cost(self, hours: Decimal) -> Decimal:
        return hours * self.loaded_hourly_cost_cny


def _dec(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PolicyError(f"{label} 必须是数值，实际为 {type(value).__name__}")
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - 需转成统一配置错误
        raise PolicyError(f"{label} 无法解析为数值: {value!r}") from exc


def _non_negative(value: Decimal, label: str) -> Decimal:
    if value < 0:
        raise PolicyError(f"{label} 不能为负: {value}")
    return value


def _fraction(value: Decimal, label: str) -> Decimal:
    if not (Decimal("0") <= value <= Decimal("1")):
        raise PolicyError(f"{label} 必须在 [0, 1] 区间: {value}")
    return value


def _factor_map(raw: Any, label: str, *, allow_zero: bool = True) -> dict[str, Decimal]:
    if not isinstance(raw, dict) or not raw:
        raise PolicyError(f"{label} 必须是至少含一项的对象")
    result: dict[str, Decimal] = {}
    for key, value in raw.items():
        dec = _fraction(_dec(value, f"{label}.{key}"), f"{label}.{key}")
        if not allow_zero and dec == 0:
            raise PolicyError(f"{label}.{key} 不能为 0")
        result[str(key)] = dec
    return result


def load_policy(path: str | Path | None = None) -> Policy:
    policy_path = Path(path) if path is not None else DEFAULT_POLICY_PATH
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyError(f"政策文件不存在: {policy_path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"政策文件不是合法 JSON: {policy_path}: {exc}") from exc
    return policy_from_mapping(raw)


def policy_from_mapping(raw: Mapping[str, Any]) -> Policy:
    required = [
        "policy_version",
        "currency",
        "loaded_hourly_cost_cny",
        "annualization_months",
        "default_period_months",
        "default_budget_cny",
        "default_group_capacity",
        "evidence_confidence",
        "governance_factor",
        "owner_factor",
        "readiness_factor",
        "budget_sensitivity_multipliers",
        "selection",
    ]
    missing = [key for key in required if key not in raw]
    if missing:
        raise PolicyError(f"政策缺少字段: {', '.join(missing)}")

    annualization = int(raw["annualization_months"])
    if annualization <= 0:
        raise PolicyError("annualization_months 必须为正整数")
    period = int(raw["default_period_months"])
    if period <= 0:
        raise PolicyError("default_period_months 必须为正整数")

    capacity_raw = raw.get("capacity_by_group", {})
    if not isinstance(capacity_raw, dict):
        raise PolicyError("capacity_by_group 必须是对象")
    capacity_by_group = {str(k): int(v) for k, v in capacity_raw.items()}
    if any(v < 0 for v in capacity_by_group.values()):
        raise PolicyError("capacity_by_group 不能为负")

    reductions = _factor_map(raw.get("assumed_reduction_rate", {}), "assumed_reduction_rate")
    if not reductions:
        raise PolicyError("assumed_reduction_rate 不能为空")

    multipliers = raw["budget_sensitivity_multipliers"]
    if not isinstance(multipliers, list) or not multipliers:
        raise PolicyError("budget_sensitivity_multipliers 必须是非空数组")
    parsed_multipliers = tuple(
        _non_negative(_dec(v, "budget_sensitivity_multipliers[]"), "budget_sensitivity_multipliers[]")
        for v in multipliers
    )
    if any(v == 0 for v in parsed_multipliers):
        raise PolicyError("budget_sensitivity_multipliers 不能含 0")

    selection_raw = raw["selection"]
    if not isinstance(selection_raw, dict):
        raise PolicyError("selection 必须是对象")

    return Policy(
        policy_version=str(raw["policy_version"]),
        currency=str(raw["currency"]),
        loaded_hourly_cost_cny=_non_negative(
            _dec(raw["loaded_hourly_cost_cny"], "loaded_hourly_cost_cny"), "loaded_hourly_cost_cny"
        ),
        annualization_months=annualization,
        default_period_months=period,
        default_budget_cny=_non_negative(_dec(raw["default_budget_cny"], "default_budget_cny"), "default_budget_cny"),
        default_group_capacity=int(raw["default_group_capacity"]),
        capacity_by_group=capacity_by_group,
        assumed_reduction_rate=reductions,
        evidence_confidence=_factor_map(raw["evidence_confidence"], "evidence_confidence"),
        governance_factor=_factor_map(raw["governance_factor"], "governance_factor"),
        owner_factor=_factor_map(raw["owner_factor"], "owner_factor"),
        readiness_factor=_factor_map(raw["readiness_factor"], "readiness_factor"),
        budget_sensitivity_multipliers=parsed_multipliers,
        selection=SelectionRules(
            **{name: bool(selection_raw.get(name, True)) for name in SelectionRules.__dataclass_fields__}
        ),
    )
