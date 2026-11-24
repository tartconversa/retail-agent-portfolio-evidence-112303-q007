"""结果回看：把试点测量对照登记时的验收口径，给出达标/未达标/数据不足的判定。

判定只使用登记时的验收定义（指标、方向、阈值、测量起点），不事后改口径倒推结论。
数据不足或测量冲突一律不算达标。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .evidence import EvidenceProfile, TIER_CONFLICTED, TIER_INVALID, TIER_NO_MEASUREMENT
from .models import Dataset, PilotResult
from .registry import Acceptance, ScenarioRecord

VERDICT_MET = "met"
VERDICT_MISSED = "missed"
VERDICT_INSUFFICIENT = "insufficient"
VERDICT_LABELS = {
    VERDICT_MET: "达标",
    VERDICT_MISSED: "未达标",
    VERDICT_INSUFFICIENT: "数据不足",
}


@dataclass(frozen=True)
class ReviewOutcome:
    scenario_id: str
    verdict: str
    metric: str | None
    direction: str | None
    threshold: Decimal | None
    observed_value: Decimal | None
    observed_unit_label: str
    rationale: str
    usable_rows: tuple[str, ...]

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)


def _metric_value(metric: str, rows: list[PilotResult], volume: Decimal | None) -> tuple[Decimal | None, str]:
    """按指标口径从试点记录计算值；未知指标返回 None。"""
    if not rows:
        return None, ""
    if metric in {"unit-hours", "hours-per-case"}:
        rates = [row.observed_hours / row.observed_volume for row in rows if row.observed_volume > 0]
        return (sum(rates) / len(rates), "小时/件") if rates else (None, "")
    if metric in {"outcome", "outcome-value", "kpi"}:
        values = [row.outcome_value for row in rows]
        return sum(values) / len(values), "结果值"
    if metric in {"total-hours", "hours"}:
        return sum((row.observed_hours for row in rows), Decimal("0")), "小时"
    if metric in {"volume", "throughput"}:
        return sum((row.observed_volume for row in rows), Decimal("0")), "件"
    if metric in {"error-rate", "error"}:
        values = [row.outcome_value for row in rows]
        return sum(values) / len(values), "比例"
    return None, ""


def review_result(
    scenario_id: str,
    record: ScenarioRecord | None,
    dataset: Dataset,
    evidence: EvidenceProfile,
    *,
    period: str | None = None,
) -> ReviewOutcome:
    if record is None or record.acceptance is None:
        return ReviewOutcome(
            scenario_id=scenario_id,
            verdict=VERDICT_INSUFFICIENT,
            metric=None,
            direction=None,
            threshold=None,
            observed_value=None,
            observed_unit_label="",
            rationale="场景未登记验收口径，无法判定；须先补充验收指标与阈值。",
            usable_rows=(),
        )
    acceptance: Acceptance = record.acceptance
    rows = dataset.pilots_for(scenario_id)
    if acceptance.measured_from:
        rows = [row for row in rows if row.period >= acceptance.measured_from]
    if period is not None:
        rows = [row for row in rows if row.period == period]
    if evidence.tier in {TIER_CONFLICTED, TIER_INVALID, TIER_NO_MEASUREMENT}:
        return ReviewOutcome(
            scenario_id=scenario_id,
            verdict=VERDICT_INSUFFICIENT,
            metric=acceptance.metric,
            direction=acceptance.direction,
            threshold=acceptance.threshold,
            observed_value=None,
            observed_unit_label="",
            rationale=f"证据等级为 {evidence.tier_label}，测量不可复核，暂不判定。",
            usable_rows=tuple(row.result_id for row in rows),
        )

    value, unit = _metric_value(acceptance.metric, rows, None)
    if value is None:
        return ReviewOutcome(
            scenario_id=scenario_id,
            verdict=VERDICT_INSUFFICIENT,
            metric=acceptance.metric,
            direction=acceptance.direction,
            threshold=acceptance.threshold,
            observed_value=None,
            observed_unit_label="",
            rationale=f"验收指标 {acceptance.metric!r} 无法从试点数据计算，需补充测量。",
            usable_rows=tuple(row.result_id for row in rows),
        )

    met = value >= acceptance.threshold if acceptance.direction == "at-least" else value <= acceptance.threshold
    comparison = "≥" if acceptance.direction == "at-least" else "≤"
    rationale = (
        f"观察值 {value.quantize(Decimal('0.0001'))} {unit} "
        f"{'达到' if met else '未达到'}约定 {comparison} {acceptance.threshold} {acceptance.unit}"
    )
    return ReviewOutcome(
        scenario_id=scenario_id,
        verdict=VERDICT_MET if met else VERDICT_MISSED,
        metric=acceptance.metric,
        direction=acceptance.direction,
        threshold=acceptance.threshold,
        observed_value=value,
        observed_unit_label=unit,
        rationale=rationale,
        usable_rows=tuple(row.result_id for row in rows),
    )
