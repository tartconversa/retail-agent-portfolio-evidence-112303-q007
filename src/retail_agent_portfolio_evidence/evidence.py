"""证据评估：把原始试点记录转成可解释的证据等级与观察区间。

关键判断：
- 同一 (场景, 期间, 数据版本) 出现多条相互矛盾的测量，不做平均或挑选，
  记为 ``conflicted``，并向上传递不确定性。
- ``baseline-missing`` 表示该次试点的对照基线不完整，不能把结果当作已观察节省。
- 试点业务量与月度基线业务量差异过大时，单位工时虽可计算，但不能直接外推到
  全量业务，故标记为口径不可比、不可货币化。
- 观察到的单位工时若高于基线，仍如实呈现负向信号，而不是裁剪为 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Dataset, PilotResult, Scenario
from .policy import Policy

TIER_OBSERVED = "observed"
TIER_PARTIAL = "partial"
TIER_CONFLICTED = "conflicted"
TIER_MISSING_BASELINE = "missing_baseline"
TIER_NO_MEASUREMENT = "no_measurement"
TIER_INVALID = "invalid"

TIER_LABELS = {
    TIER_OBSERVED: "已观察",
    TIER_PARTIAL: "部分测量",
    TIER_CONFLICTED: "测量冲突",
    TIER_MISSING_BASELINE: "缺少基线",
    TIER_NO_MEASUREMENT: "无测量",
    TIER_INVALID: "基线不可用",
}

FLAG_LABELS = {
    "SCOPE_VOLUME_MISMATCH": "试点业务量与基线口径不匹配，观察结果不可直接外推",
    "PERIOD_BASIS_MISMATCH": "试点期间与月度基线期间不一致，需按期间长度换算",
    "OBSERVED_WORSE_THAN_BASELINE": "观察到的单位工时高于基线，未体现节省",
    "CONFLICTING_MEASUREMENTS": "同一口径存在多条相互矛盾的测量",
    "BASELINE_MISSING": "试点缺少可比基线",
    "NO_MEASUREMENT": "没有试点测量记录",
    "PARTIAL_MEASUREMENT": "测量仅覆盖部分业务",
    "ZERO_VOLUME": "月度业务量为 0，无法形成单位口径",
    "ZERO_BASELINE_HOURS": "基线工时为 0，无法归因节省",
    "VERSION_SPLIT": "试点结果来自多个数据版本",
}

VOLUME_SCOPE_LOW = Decimal("0.5")
VOLUME_SCOPE_HIGH = Decimal("3")


def period_months(period: str) -> int | None:
    """把期间标签换算为月数；无法识别返回 None。"""
    label = period.strip().upper()
    if len(label) >= 6 and label[-2] == "Q" and label[-1] in "1234" and label[:4].isdigit():
        return 3
    if len(label) == 7 and label[4] == "-" and label[:4].isdigit() and label[5:].isdigit():
        month = int(label[5:])
        return 1 if 1 <= month <= 12 else None
    return None


@dataclass(frozen=True)
class ObservationGroup:
    period: str
    source_version: str
    measurement_states: tuple[str, ...]
    period_months: int | None
    volume_low: Decimal
    volume_high: Decimal
    monthly_volume_low: Decimal | None
    monthly_volume_high: Decimal | None
    unit_hours_low: Decimal
    unit_hours_high: Decimal
    consistent: bool
    row_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceProfile:
    scenario_id: str
    tier: str
    effective_confidence: Decimal
    baseline_unit_hours: Decimal | None
    observations: tuple[ObservationGroup, ...]
    observed_unit_hours_conservative: Decimal | None
    observed_unit_hours_best: Decimal | None
    observed_delta_conservative: Decimal | None
    observed_delta_best: Decimal | None
    observed_monthly_hours_conservative: Decimal | None
    observed_annual_value_conservative: Decimal | None
    observed_monetizable: bool
    assumption_status: str
    flags: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, self.tier)

    @property
    def is_verified(self) -> bool:
        return self.tier == TIER_OBSERVED and self.observed_monetizable


def _group_pilots(rows: list[PilotResult]) -> list[ObservationGroup]:
    grouped: dict[tuple[str, str], list[PilotResult]] = {}
    for row in rows:
        grouped.setdefault((row.period, row.source_version), []).append(row)
    groups: list[ObservationGroup] = []
    for (period, version), items in grouped.items():
        unit_rates = [row.observed_hours / row.observed_volume for row in items if row.observed_volume > 0]
        volumes = [row.observed_volume for row in items]
        consistent = len({(row.observed_volume, row.observed_hours) for row in items}) == 1
        months = period_months(period)
        groups.append(
            ObservationGroup(
                period=period,
                source_version=version,
                measurement_states=tuple(sorted({row.measurement_state for row in items})),
                period_months=months,
                volume_low=min(volumes),
                volume_high=max(volumes),
                monthly_volume_low=min(volumes) / months if months else None,
                monthly_volume_high=max(volumes) / months if months else None,
                unit_hours_low=min(unit_rates) if unit_rates else Decimal("0"),
                unit_hours_high=max(unit_rates) if unit_rates else Decimal("0"),
                consistent=consistent,
                row_ids=tuple(sorted(row.result_id for row in items)),
            )
        )
    groups.sort(key=lambda group: (group.period, group.source_version))
    return groups


def assess_evidence(scenario: Scenario, dataset: Dataset, policy: Policy) -> EvidenceProfile:
    pilots = dataset.pilots_for(scenario.scenario_id)
    groups = _group_pilots(pilots)
    baseline_unit = scenario.baseline_unit_hours
    flags: list[str] = []
    notes: list[str] = []

    usable_baseline = (
        baseline_unit is not None
        and baseline_unit > 0
        and scenario.monthly_volume > 0
        and scenario.baseline_hours > 0
    )
    if scenario.monthly_volume == 0:
        flags.append("ZERO_VOLUME")
    if scenario.baseline_hours == 0:
        flags.append("ZERO_BASELINE_HOURS")

    period_labels = {group.period_months for group in groups}
    if period_labels != {1} and groups:
        flags.append("PERIOD_BASIS_MISMATCH")
        notes.append(
            "试点期间不是单月（如季度），已按期间长度换算为月度可比口径；"
            "若期间内业务结构变化，换算值仍可能偏离月度基线。"
        )

    has_conflict = any(not group.consistent for group in groups) or any(
        len(group.measurement_states) > 1 for group in groups
    )
    if has_conflict:
        flags.append("CONFLICTING_MEASUREMENTS")
    if len({row.source_version for row in pilots}) > 1:
        flags.append("VERSION_SPLIT")

    states = {row.measurement_state for row in pilots}
    if not pilots:
        flags.append("NO_MEASUREMENT")
    if "partial" in states:
        flags.append("PARTIAL_MEASUREMENT")
    if "baseline-missing" in states:
        flags.append("BASELINE_MISSING")

    if not usable_baseline:
        tier = TIER_INVALID
    elif not pilots:
        tier = TIER_NO_MEASUREMENT
    elif has_conflict:
        tier = TIER_CONFLICTED
    elif "baseline-missing" in states:
        tier = TIER_MISSING_BASELINE
    elif "partial" in states:
        tier = TIER_PARTIAL
    else:
        tier = TIER_OBSERVED

    observ_low = observ_high = None
    delta_conservative = delta_best = None
    monthly_hours = None
    annual_value = None
    monetizable = False

    if groups and usable_baseline:
        observ_low = min(group.unit_hours_low for group in groups)
        observ_high = max(group.unit_hours_high for group in groups)
        assert baseline_unit is not None
        delta_conservative = (baseline_unit - observ_high) / baseline_unit
        delta_best = (baseline_unit - observ_low) / baseline_unit
        monthly_hours = scenario.monthly_volume * (baseline_unit - observ_high)
        annual_value = policy.scenario_hour_cost(monthly_hours * 12)

        if any(
            group.monthly_volume_high is not None
            and (
                group.monthly_volume_high < scenario.monthly_volume * VOLUME_SCOPE_LOW
                or group.monthly_volume_high > scenario.monthly_volume * VOLUME_SCOPE_HIGH
            )
            for group in groups
        ):
            flags.append("SCOPE_VOLUME_MISMATCH")
            notes.append(
                "试点期间业务量（换算为月度）与月度基线业务量差异过大，"
                "单位工时只能作方向参考，不能把观察到的节省按全量业务货币化。"
            )
        if delta_best is not None and delta_best < 0:
            flags.append("OBSERVED_WORSE_THAN_BASELINE")
            notes.append("即使取最有利的一条测量，试点单位工时仍高于基线；该场景的节省假设未被观察支持。")
        monetizable = tier == TIER_OBSERVED and "SCOPE_VOLUME_MISMATCH" not in flags

    base_confidence = policy.evidence_confidence.get(tier, Decimal("0"))
    penalty = Decimal("1")
    if "SCOPE_VOLUME_MISMATCH" in flags:
        penalty *= Decimal("0.70")
    if "OBSERVED_WORSE_THAN_BASELINE" in flags:
        penalty *= Decimal("0.60")
    effective = max((base_confidence * penalty).quantize(Decimal("0.0001")), Decimal("0.05"))

    unique_flags = tuple(dict.fromkeys(flags))
    if delta_best is not None and delta_best < 0:
        assumption_status = "contradicted"
    elif delta_conservative is None:
        assumption_status = "unknown"
    elif "SCOPE_VOLUME_MISMATCH" in unique_flags:
        assumption_status = "unverified"
    else:
        assumption_status = "supported"

    return EvidenceProfile(
        scenario_id=scenario.scenario_id,
        tier=tier,
        effective_confidence=effective,
        baseline_unit_hours=baseline_unit if usable_baseline else None,
        observations=tuple(groups),
        observed_unit_hours_conservative=observ_high,
        observed_unit_hours_best=observ_low,
        observed_delta_conservative=delta_conservative,
        observed_delta_best=delta_best,
        observed_monthly_hours_conservative=monthly_hours,
        observed_annual_value_conservative=annual_value,
        observed_monetizable=monetizable,
        assumption_status=assumption_status,
        flags=unique_flags,
        notes=tuple(notes),
    )
