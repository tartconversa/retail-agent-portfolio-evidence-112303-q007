"""可持久化登记：场景登记、验收口径、阶段决策日志与结果回看。

所有写入采用追加式 JSON Lines 与原子替换，保证：
- 决策历史不可被静默覆盖，可回溯到原始场景定义；
- 暂停/重评只影响"未释放"资金，已释放与已形成的合同金额保留；
- 每条记录保留评估时的政策版本与输入版本，后续结果可对照当时口径解释。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

REGISTRY_VERSION = "1"

DECISION_KINDS = {
    "register": "登记场景与验收口径",
    "advance": "推进至下一阶段",
    "hold": "暂缓",
    "release": "释放阶段资金",
    "pause": "暂停未释放资金",
    "revise": "修订验收口径",
    "stop": "停止场景",
    "note": "补充记录",
}


class RegistryError(ValueError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"无法序列化 {type(value).__name__}")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Acceptance:
    metric: str
    direction: str
    threshold: Decimal
    unit: str
    measured_from: str
    confirmed_by: str
    confirmed_on: date

    def validate(self) -> None:
        if self.direction not in {"at-least", "at-most"}:
            raise RegistryError(f"验收方向必须是 at-least 或 at-most，实际 {self.direction!r}")
        if not self.metric.strip():
            raise RegistryError("验收指标名不能为空")
        if not self.confirmed_by.strip():
            raise RegistryError("验收口径必须记录确认人")


@dataclass
class ScenarioRecord:
    scenario_id: str
    acceptance: Acceptance | None = None
    decision_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def latest_decision(self) -> dict[str, Any] | None:
        return self.decision_log[-1] if self.decision_log else None

    @property
    def current_stage(self) -> str:
        for entry in reversed(self.decision_log):
            stage = entry.get("stage")
            if stage:
                return str(stage)
        return "unregistered"

    @property
    def funds_released_total(self) -> Decimal:
        total = Decimal("0")
        for entry in self.decision_log:
            if entry.get("action") == "release":
                total += Decimal(str(entry.get("amount", "0")))
        return total

    @property
    def funds_paused_total(self) -> Decimal:
        total = Decimal("0")
        for entry in self.decision_log:
            if entry.get("action") == "pause":
                total += Decimal(str(entry.get("amount", "0")))
        return total

    @property
    def active(self) -> bool:
        latest = self.latest_decision
        return latest is None or latest.get("action") not in {"stop"}


class Registry:
    """管理一个登记目录：scenarios.json 保存场景与验收口径，decisions.jsonl 追加决策。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.scenarios_path = self.root / "scenarios.json"
        self.decisions_path = self.root / "decisions.jsonl"

    def exists(self) -> bool:
        return self.scenarios_path.exists()

    def load(self) -> dict[str, ScenarioRecord]:
        if not self.scenarios_path.exists():
            return {}
        raw = _read_json(self.scenarios_path)
        if raw.get("registry_version") != REGISTRY_VERSION:
            raise RegistryError(
                f"登记版本不兼容：文件为 {raw.get('registry_version')!r}，程序支持 {REGISTRY_VERSION!r}"
            )
        records: dict[str, ScenarioRecord] = {}
        for item in raw.get("scenarios", []):
            acceptance = None
            if item.get("acceptance"):
                payload = item["acceptance"]
                acceptance = Acceptance(
                    metric=payload["metric"],
                    direction=payload["direction"],
                    threshold=Decimal(str(payload["threshold"])),
                    unit=payload["unit"],
                    measured_from=payload["measured_from"],
                    confirmed_by=payload["confirmed_by"],
                    confirmed_on=date.fromisoformat(payload["confirmed_on"]),
                )
            records[item["scenario_id"]] = ScenarioRecord(
                scenario_id=item["scenario_id"],
                acceptance=acceptance,
            )
        for entry in self._read_decisions():
            record = records.setdefault(entry["scenario_id"], ScenarioRecord(entry["scenario_id"]))
            record.decision_log.append(entry)
        return records

    def _read_decisions(self) -> list[dict[str, Any]]:
        if not self.decisions_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.decisions_path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise RegistryError(f"decisions.jsonl 第 {number} 行不是合法 JSON: {exc}") from exc
        return entries

    def save(self, records: dict[str, ScenarioRecord]) -> None:
        payload = {
            "registry_version": REGISTRY_VERSION,
            "scenarios": [
                {
                    "scenario_id": record.scenario_id,
                    "acceptance": asdict(record.acceptance) if record.acceptance else None,
                }
                for record in sorted(records.values(), key=lambda r: r.scenario_id)
            ],
        }
        _atomic_write(self.scenarios_path, json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n")

    def append_decision(self, entry: dict[str, Any]) -> None:
        action = entry.get("action")
        if action not in DECISION_KINDS:
            raise RegistryError(f"未知决策动作 {action!r}，允许 {sorted(DECISION_KINDS)}")
        entry = dict(entry)
        entry.setdefault("recorded_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        line = json.dumps(entry, ensure_ascii=False, default=_json_default, sort_keys=True)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.decisions_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record(
        self,
        records: dict[str, ScenarioRecord],
        *,
        action: str,
        scenario_id: str,
        rationale: str,
        actor: str,
        stage: str | None = None,
        amount: Decimal | None = None,
        policy_version: str | None = None,
        evidence_tier: str | None = None,
        snapshot: dict[str, Any] | None = None,
        acceptance: Acceptance | None = None,
    ) -> dict[str, Any]:
        if not rationale.strip():
            raise RegistryError("决策必须写明理由")
        if not actor.strip():
            raise RegistryError("决策必须记录责任人")
        if action == "release" and (amount is None or amount <= 0):
            raise RegistryError("释放资金必须给出正金额")
        if acceptance is not None:
            acceptance.validate()
        record = records.setdefault(scenario_id, ScenarioRecord(scenario_id))
        if acceptance is not None:
            if action not in {"register", "revise"}:
                raise RegistryError("只有 register 或 revise 可以设置验收口径")
            if action == "register" and record.acceptance is not None:
                raise RegistryError("场景已登记验收口径，请使用 revise 修订")
            record.acceptance = acceptance
        entry: dict[str, Any] = {
            "action": action,
            "scenario_id": scenario_id,
            "rationale": rationale,
            "actor": actor,
        }
        if stage:
            entry["stage"] = stage
        if amount is not None:
            entry["amount"] = amount
        if policy_version:
            entry["policy_version"] = policy_version
        if evidence_tier:
            entry["evidence_tier"] = evidence_tier
        if snapshot:
            entry["snapshot"] = snapshot
        self.append_decision(entry)
        record.decision_log.append(entry)
        return entry


def pause_check(
    record: ScenarioRecord | None,
    *,
    required: Decimal,
    released_from_data: Decimal,
) -> tuple[Decimal, Decimal, str]:
    """计算可暂停的未释放资金，并明确保留部分。

    规则：已释放/已形成合同的金额不可追回；仅未释放余额可暂停。
    ``released_from_data`` 为资料中已释放金额，登记日志中的释放额可能更高，取二者较大值。
    """
    if record is None:
        preserved = min(released_from_data, required)
        outstanding = max(required - preserved, Decimal("0"))
        return outstanding, preserved, _pause_note(outstanding, preserved)
    preserved = max(released_from_data, record.funds_released_total)
    preserved = min(preserved, required)
    paused_already = record.funds_paused_total
    outstanding = max(required - preserved - paused_already, Decimal("0"))
    return outstanding, preserved, _pause_note(outstanding, preserved)


def _pause_note(outstanding: Decimal, preserved: Decimal) -> str:
    return f"保留已释放/已形成合同 {preserved}；可暂停未释放 {outstanding}"
