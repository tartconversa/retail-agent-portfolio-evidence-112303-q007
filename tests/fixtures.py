"""测试夹具：在临时目录生成最小但完整的合成资料集。

测试不依赖仓库内的 data/，每个用例都能精确控制测量矛盾、基线缺失、
资金超额释放等场景，避免测试与示例数据耦合。
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

COLUMNS = {
    "candidate_scenarios.csv": [
        "scenario_id", "department", "process", "monthly_volume",
        "baseline_hours", "error_rate", "target_kpi", "owner_state",
    ],
    "dependencies.csv": [
        "scenario_id", "dependency", "capacity_units", "shared_group",
        "readiness_state", "version",
    ],
    "governance_reviews.csv": [
        "review_id", "scenario_id", "data_scope", "purpose",
        "risk_level", "decision", "reviewed_at",
    ],
    "funding_stages.csv": [
        "stage_id", "scenario_id", "amount", "release_condition",
        "committee_decision", "released_amount", "result_state",
    ],
    "pilot_results.csv": [
        "result_id", "scenario_id", "period", "observed_volume",
        "observed_hours", "outcome_value", "measurement_state", "source_version",
    ],
}


@dataclass
class FixtureBuilder:
    """按场景累积记录，最后一次性写出五个 CSV。"""

    scenarios: list[dict[str, str]] = field(default_factory=list)
    dependencies: list[dict[str, str]] = field(default_factory=list)
    governance: list[dict[str, str]] = field(default_factory=list)
    funding: list[dict[str, str]] = field(default_factory=list)
    pilots: list[dict[str, str]] = field(default_factory=list)
    _counters: dict[str, int] = field(default_factory=dict)

    def _next(self, prefix: str, table: str) -> int:
        index = self._counters.get(table, 0) + 1
        self._counters[table] = index
        return index

    def scenario(
        self,
        scenario_id: str,
        *,
        monthly_volume: int = 1000,
        baseline_hours: int = 30,
        error_rate: str = "0.02",
        target_kpi: str = "rework",
        owner_state: str = "sponsor-confirmed",
        department: str = "service",
        process: str = "case-intake",
    ) -> "FixtureBuilder":
        self.scenarios.append(
            {
                "scenario_id": scenario_id,
                "department": department,
                "process": process,
                "monthly_volume": str(monthly_volume),
                "baseline_hours": str(baseline_hours),
                "error_rate": error_rate,
                "target_kpi": target_kpi,
                "owner_state": owner_state,
            }
        )
        return self

    def dependency(
        self,
        scenario_id: str,
        *,
        dependency: str = "workflow",
        capacity_units: int = 4,
        shared_group: str = "SH-01",
        readiness_state: str = "ready",
        version: str = "V-1",
    ) -> "FixtureBuilder":
        self.dependencies.append(
            {
                "scenario_id": scenario_id,
                "dependency": dependency,
                "capacity_units": str(capacity_units),
                "shared_group": shared_group,
                "readiness_state": readiness_state,
                "version": version,
            }
        )
        return self

    def review(
        self,
        scenario_id: str,
        *,
        decision: str = "approved",
        risk_level: str = "low",
        reviewed_at: str = "2026-11-02",
        data_scope: str = "customer",
        purpose: str = "service",
    ) -> "FixtureBuilder":
        index = self._next("GR", "governance")
        self.governance.append(
            {
                "review_id": f"GR-{index:03d}",
                "scenario_id": scenario_id,
                "data_scope": data_scope,
                "purpose": purpose,
                "risk_level": risk_level,
                "decision": decision,
                "reviewed_at": reviewed_at,
            }
        )
        return self

    def stage(
        self,
        scenario_id: str,
        *,
        amount: int = 20000,
        release_condition: str = "owner-commitment",
        committee_decision: str = "pending",
        released_amount: int = 0,
        result_state: str = "not-started",
    ) -> "FixtureBuilder":
        index = self._next("FS", "funding")
        self.funding.append(
            {
                "stage_id": f"FS-{index:03d}",
                "scenario_id": scenario_id,
                "amount": str(amount),
                "release_condition": release_condition,
                "committee_decision": committee_decision,
                "released_amount": str(released_amount),
                "result_state": result_state,
            }
        )
        return self

    def pilot(
        self,
        scenario_id: str,
        *,
        period: str = "2026-Q2",
        observed_volume: int = 3000,
        observed_hours: int = 60,
        outcome_value: str = "0.2",
        measurement_state: str = "complete",
        source_version: str = "MV-1",
    ) -> "FixtureBuilder":
        index = self._next("PR", "pilots")
        self.pilots.append(
            {
                "result_id": f"PR-{index:03d}",
                "scenario_id": scenario_id,
                "period": period,
                "observed_volume": str(observed_volume),
                "observed_hours": str(observed_hours),
                "outcome_value": outcome_value,
                "measurement_state": measurement_state,
                "source_version": source_version,
            }
        )
        return self

    def complete_scenario(self, scenario_id: str, **kwargs) -> "FixtureBuilder":
        """创建一个闸门全部可通过的默认场景。"""
        self.scenario(scenario_id, **kwargs)
        self.dependency(scenario_id)
        self.review(scenario_id)
        self.stage(scenario_id)
        self.pilot(scenario_id)
        return self

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        for filename, rows in (
            ("candidate_scenarios.csv", self.scenarios),
            ("dependencies.csv", self.dependencies),
            ("governance_reviews.csv", self.governance),
            ("funding_stages.csv", self.funding),
            ("pilot_results.csv", self.pilots),
        ):
            with (directory / filename).open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=COLUMNS[filename])
                writer.writeheader()
                writer.writerows(rows)
        return directory
