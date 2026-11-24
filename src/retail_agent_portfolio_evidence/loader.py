"""CSV 资料加载与结构校验。

设计原则：
- 结构错误（缺列、无法解析的数值、孤立的场景引用）直接失败，因为没有可信的修正口径。
- 业务语义异常（同一衡量口径重复且相互矛盾、已释放金额超过授权、治理复审互相覆盖）
  不擅自取均值或最大值，而是记录为数据问题，交给评估层按保守口径处理。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import (
    Dataset,
    Dependency,
    FundingStage,
    GovernanceReview,
    PilotResult,
    Scenario,
)


class DataLoadError(ValueError):
    """资料结构不符合契约。"""


@dataclass(frozen=True)
class DataIssue:
    code: str
    scenario_id: str
    detail: str


REQUIRED_COLUMNS = {
    "candidate_scenarios.csv": [
        "scenario_id",
        "department",
        "process",
        "monthly_volume",
        "baseline_hours",
        "error_rate",
        "target_kpi",
        "owner_state",
    ],
    "dependencies.csv": [
        "scenario_id",
        "dependency",
        "capacity_units",
        "shared_group",
        "readiness_state",
        "version",
    ],
    "governance_reviews.csv": [
        "review_id",
        "scenario_id",
        "data_scope",
        "purpose",
        "risk_level",
        "decision",
        "reviewed_at",
    ],
    "funding_stages.csv": [
        "stage_id",
        "scenario_id",
        "amount",
        "release_condition",
        "committee_decision",
        "released_amount",
        "result_state",
    ],
    "pilot_results.csv": [
        "result_id",
        "scenario_id",
        "period",
        "observed_volume",
        "observed_hours",
        "outcome_value",
        "measurement_state",
        "source_version",
    ],
}

KNOWN_STATES = {
    "owner_state": {"sponsor-confirmed", "proposed", "paused"},
    "readiness_state": {"ready", "partial", "blocked"},
    "decision": {"approved", "limited", "revise"},
    "committee_decision": {"approved", "held", "pending"},
    "measurement_state": {"complete", "partial", "baseline-missing"},
}


def _decimal(raw: str, filename: str, line: int, column: str) -> Decimal:
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError) as exc:
        raise DataLoadError(f"{filename}:{line} 列 {column} 不是数值: {raw!r}") from exc
    if not value.is_finite():
        raise DataLoadError(f"{filename}:{line} 列 {column} 不是有限数值: {raw!r}")
    return value


def _int(raw: str, filename: str, line: int, column: str) -> int:
    value = _decimal(raw, filename, line, column)
    if value != value.to_integral_value():
        raise DataLoadError(f"{filename}:{line} 列 {column} 需要整数: {raw!r}")
    return int(value)


def _date(raw: str, filename: str, line: int, column: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise DataLoadError(f"{filename}:{line} 列 {column} 不是 ISO 日期: {raw!r}") from exc


def _rows(path: Path) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fieldnames = list(reader.fieldnames or [])
            rows = [(index, row) for index, row in enumerate(reader, start=2)]
    except FileNotFoundError as exc:
        raise DataLoadError(f"资料文件不存在: {path}") from exc
    expected = REQUIRED_COLUMNS[path.name]
    if fieldnames != expected:
        raise DataLoadError(f"{path.name} 列不符：期望 {expected}，实际 {fieldnames}")
    return fieldnames, rows


def _check_state(value: str, column: str, filename: str, line: int) -> None:
    allowed = KNOWN_STATES.get(column)
    if allowed is not None and value not in allowed:
        raise DataLoadError(f"{filename}:{line} 列 {column} 取值未知: {value!r}，允许 {sorted(allowed)}")


def load_dataset(data_dir: str | Path) -> tuple[Dataset, list[DataIssue]]:
    directory = Path(data_dir)
    dataset = Dataset()
    issues: list[DataIssue] = []

    _, scenario_rows = _rows(directory / "candidate_scenarios.csv")
    for line, row in scenario_rows:
        _check_state(row["owner_state"], "owner_state", "candidate_scenarios.csv", line)
        scenario = Scenario(
            scenario_id=row["scenario_id"],
            department=row["department"],
            process=row["process"],
            monthly_volume=_decimal(row["monthly_volume"], "candidate_scenarios.csv", line, "monthly_volume"),
            baseline_hours=_decimal(row["baseline_hours"], "candidate_scenarios.csv", line, "baseline_hours"),
            error_rate=_decimal(row["error_rate"], "candidate_scenarios.csv", line, "error_rate"),
            target_kpi=row["target_kpi"],
            owner_state=row["owner_state"],
        )
        if scenario.scenario_id in dataset.scenarios:
            raise DataLoadError(f"candidate_scenarios.csv:{line} 场景重复: {scenario.scenario_id}")
        if scenario.monthly_volume < 0 or scenario.baseline_hours < 0 or scenario.error_rate < 0:
            raise DataLoadError(f"candidate_scenarios.csv:{line} 负数业务量/工时/错误率: {scenario.scenario_id}")
        dataset.add_scenario(scenario)

    _, dependency_rows = _rows(directory / "dependencies.csv")
    for line, row in dependency_rows:
        _check_state(row["readiness_state"], "readiness_state", "dependencies.csv", line)
        dataset.dependencies.append(
            Dependency(
                scenario_id=row["scenario_id"],
                dependency=row["dependency"],
                capacity_units=_int(row["capacity_units"], "dependencies.csv", line, "capacity_units"),
                shared_group=row["shared_group"],
                readiness_state=row["readiness_state"],
                version=row["version"],
            )
        )

    _, governance_rows = _rows(directory / "governance_reviews.csv")
    for line, row in governance_rows:
        _check_state(row["decision"], "decision", "governance_reviews.csv", line)
        dataset.governance.append(
            GovernanceReview(
                review_id=row["review_id"],
                scenario_id=row["scenario_id"],
                data_scope=row["data_scope"],
                purpose=row["purpose"],
                risk_level=row["risk_level"],
                decision=row["decision"],
                reviewed_at=_date(row["reviewed_at"], "governance_reviews.csv", line, "reviewed_at"),
            )
        )

    _, funding_rows = _rows(directory / "funding_stages.csv")
    for line, row in funding_rows:
        _check_state(row["committee_decision"], "committee_decision", "funding_stages.csv", line)
        stage = FundingStage(
            stage_id=row["stage_id"],
            scenario_id=row["scenario_id"],
            amount=_decimal(row["amount"], "funding_stages.csv", line, "amount"),
            release_condition=row["release_condition"],
            committee_decision=row["committee_decision"],
            released_amount=_decimal(row["released_amount"], "funding_stages.csv", line, "released_amount"),
            result_state=row["result_state"],
        )
        if stage.amount < 0 or stage.released_amount < 0:
            raise DataLoadError(f"funding_stages.csv:{line} 金额为负: {stage.stage_id}")
        dataset.funding.append(stage)

    _, pilot_rows = _rows(directory / "pilot_results.csv")
    for line, row in pilot_rows:
        _check_state(row["measurement_state"], "measurement_state", "pilot_results.csv", line)
        dataset.pilots.append(
            PilotResult(
                result_id=row["result_id"],
                scenario_id=row["scenario_id"],
                period=row["period"],
                observed_volume=_decimal(row["observed_volume"], "pilot_results.csv", line, "observed_volume"),
                observed_hours=_decimal(row["observed_hours"], "pilot_results.csv", line, "observed_hours"),
                outcome_value=_decimal(row["outcome_value"], "pilot_results.csv", line, "outcome_value"),
                measurement_state=row["measurement_state"],
                source_version=row["source_version"],
            )
        )

    issues.extend(_cross_check(dataset))
    return dataset, issues


def _cross_check(dataset: Dataset) -> list[DataIssue]:
    issues: list[DataIssue] = []
    known = set(dataset.scenarios)

    for label, rows in (
        ("dependencies", dataset.dependencies),
        ("governance_reviews", dataset.governance),
        ("funding_stages", dataset.funding),
        ("pilot_results", dataset.pilots),
    ):
        orphans = sorted({row.scenario_id for row in rows} - known)
        if orphans:
            raise DataLoadError(f"{label} 引用了未登记的候选场景: {', '.join(orphans)}")

    for scenario in dataset.iter_scenarios():
        sid = scenario.scenario_id
        if scenario.monthly_volume == 0:
            issues.append(DataIssue("ZERO_VOLUME", sid, "月业务量为 0，无法形成单位工时基线"))
        if scenario.baseline_hours == 0:
            issues.append(DataIssue("ZERO_BASELINE_HOURS", sid, "基线工时为 0，无法归因节省"))

        deps = dataset.dependencies_for(sid)
        if not deps:
            issues.append(DataIssue("NO_DEPENDENCY", sid, "未登记依赖系统，实施容量未知"))
        if len({dep.version for dep in deps}) > 1:
            issues.append(DataIssue("DEPENDENCY_VERSION_SPLIT", sid, "同一场景依赖的版本号不一致"))

        gov = dataset.governance_for(sid)
        if not gov:
            issues.append(DataIssue("NO_GOVERNANCE", sid, "没有数据治理审查结论"))
        elif len(gov) > 1:
            issues.append(
                DataIssue(
                    "GOVERNANCE_SUPERSEDED",
                    sid,
                    f"存在 {len(gov)} 次审查，以最新一次为准，历史结论不叠加",
                )
            )

        stages = dataset.funding_for(sid)
        if not stages:
            issues.append(DataIssue("NO_FUNDING_STAGE", sid, "未登记资金阶段，无法判断可释放额度"))
        for stage in stages:
            if stage.released_amount > stage.amount:
                issues.append(
                    DataIssue(
                        "OVER_RELEASED",
                        sid,
                        f"{stage.stage_id} 已释放 {stage.released_amount} 超过授权 {stage.amount}，"
                        "超出部分不计入预算占用",
                    )
                )
            if stage.released_amount > 0 and stage.committee_decision != "approved":
                issues.append(
                    DataIssue(
                        "RELEASE_WITHOUT_APPROVAL",
                        sid,
                        f"{stage.stage_id} 在委员会结论为 {stage.committee_decision} 时已有释放金额",
                    )
                )
            if stage.released_amount > 0 and stage.result_state == "not-started":
                issues.append(
                    DataIssue(
                        "RELEASE_NOT_STARTED",
                        sid,
                        f"{stage.stage_id} 已释放资金但结果状态为未开始",
                    )
                )

        pilots = dataset.pilots_for(sid)
        if not pilots:
            issues.append(DataIssue("NO_PILOT_RESULT", sid, "没有试点结果，无法区分已观察与假设收益"))
        seen: dict[tuple[str, str], list[PilotResult]] = {}
        for row in pilots:
            seen.setdefault((row.period, row.source_version), []).append(row)
        for (period, version), group in seen.items():
            if len(group) > 1:
                rates = {(g.observed_volume, g.observed_hours) for g in group}
                issues.append(
                    DataIssue(
                        "DUPLICATE_MEASUREMENT",
                        sid,
                        f"{period}/{version} 有 {len(group)} 条测量"
                        + ("且数值相互矛盾" if len(rates) > 1 else "（数值一致）"),
                    )
                )
            if len({g.measurement_state for g in group}) > 1:
                issues.append(
                    DataIssue(
                        "MEASUREMENT_STATE_CONFLICT",
                        sid,
                        f"{period}/{version} 同一来源的测量状态不一致",
                    )
                )
        if len({row.source_version for row in pilots}) > 1:
            issues.append(DataIssue("PILOT_VERSION_SPLIT", sid, "试点结果来自多个数据版本"))
    return issues
