"""命令行界面：评估组合、查看场景详情、登记与决策、结果回看。

用法示例::

    python -m retail_agent_portfolio_evidence evaluate --data data
    python -m retail_agent_portfolio_evidence evaluate --budget 60000 --format json
    python -m retail_agent_portfolio_evidence show SCN-001 --data data
    python -m retail_agent_portfolio_evidence register SCN-001 --data data \
        --metric unit-hours --direction at-most --threshold 0.024 --unit hours/case \
        --measured-from 2026-Q2 --actor "业务负责人A"
    python -m retail_agent_portfolio_evidence decide advance SCN-001 --actor "投资委员会" \
        --rationale "试点指标达标，释放第二阶段"
    python -m retail_agent_portfolio_evidence review --data data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

from .appraisal import Appraisal, build_appraisal, to_json
from .evidence import FLAG_LABELS
from .gating import funding_request, next_outstanding_stage
from .policy import PolicyError
from .registry import DECISION_KINDS, Acceptance, Registry, RegistryError, pause_check
from .loader import DataLoadError

DEFAULT_REGISTRY = "registry"
DEFAULT_DATA = "data"
ENTRY_GATES = {"owner", "governance", "readiness", "funding", "baseline"}


def _money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


def _build(args: argparse.Namespace) -> Appraisal:
    return build_appraisal(
        args.data,
        policy_path=args.policy,
        registry_root=args.registry,
        budget=Decimal(str(args.budget)) if getattr(args, "budget", None) is not None else None,
        reduction_key=getattr(args, "reduction", "base"),
    )


def _print_header(title: str) -> None:
    print(title)
    print("=" * len(title))


def cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        appraisal = _build(args)
    except (PolicyError, DataLoadError, RegistryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        json.dump(to_json(appraisal), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    _render_evaluation(appraisal)
    return 0


def _render_evaluation(appraisal: Appraisal) -> None:
    policy = appraisal.policy
    portfolio = appraisal.portfolio
    _print_header("组合评估")
    print(
        f"政策版本 {policy.policy_version} · 折减档位 {appraisal.reduction_key} · "
        f"预算 {_money(portfolio.budget)} {policy.currency} · 场景 {len(appraisal.dataset.scenarios)} 个"
    )
    print()
    print(f"入选 {len(portfolio.selected)} 个场景：{'、'.join(portfolio.selected_ids) or '无'}")
    print(
        f"风险调整后年度价值合计 {_money(portfolio.total_value)}，"
        f"启动成本合计 {_money(portfolio.total_cost)}，"
        f"预算使用率 {_pct(portfolio.total_cost / portfolio.budget) if portfolio.budget else '-'}"
    )
    if portfolio.binding_groups:
        print(f"受共享实施容量约束的组：{', '.join(portfolio.binding_groups)}")
    capacity_line = " · ".join(
        f"{group} {used}/{portfolio.capacities.get(group, 0)}"
        for group, used in sorted(portfolio.group_usage.items())
    )
    if capacity_line:
        print(f"共享容量占用：{capacity_line}")
    print()

    header = (
        f"{'场景':<8}{'部门':<10}{'证据':<8}{'观察Δ单位工时':>14}{'假设年值':>12}"
        f"{'风险调整':>12}{'启动资金':>10}{'值/成本':>9}  闸门"
    )
    print(header)
    print("-" * len(header))
    for val in appraisal.valuation_rows():
        gates = "".join("✓" if gate.passed else "✗" for gate in val.gates.results)
        evidence = appraisal.evidence[val.scenario_id]
        delta = evidence.observed_delta_conservative
        observed = _pct(delta) if delta is not None else "-"
        ratio = f"{val.value_cost_ratio:.2f}" if val.value_cost_ratio is not None else "-"
        print(
            f"{val.scenario_id:<8}{val.department:<10}{val.tier_label:<8}"
            f"{observed:>14}{_money(val.assumption_annual_value):>12}"
            f"{_money(val.risk_adjusted_annual_value):>12}{_money(val.startup_funding):>10}{ratio:>9}  {gates}"
        )
    print()
    print(" 观察Δ = 试点单位工时相对基线的最保守变化；负值表示试点单位工时反而上升，即节省假设未被观察支持。")
    print("闸门顺序：" + "、".join(
        f"{index + 1}={gate.label}" for index, gate in enumerate(appraisal.valuation_rows()[0].gates.results)
    ))

    contradicted = [v for v in appraisal.ordered_valuations() if v.assumption_contradicted_by_observation]
    if contradicted:
        print()
        print(f"注意：{len(contradicted)} 个场景的试点观察方向与节省假设相反（观察单位工时高于基线），")
        print("      已按风险调整值再打 50% 折扣；它们的假设收益需要重新验证，不能视为已实现。")
        for val in contradicted[:5]:
            print(f"  - {val.scenario_id}")
        if len(contradicted) > 5:
            print(f"  - …… 另有 {len(contradicted) - 5} 个，见 evaluate --format json")

    unextrapolable = [
        v for v in appraisal.ordered_valuations()
        if not v.observed_monetizable and v.evidence_tier == "observed"
    ]
    if unextrapolable:
        print()
        print(f"注意：{len(unextrapolable)} 个场景的试点测量本身完整，但覆盖范围与月度基线不可比，")
        print("      因此观察值只能作为方向参考，不能作为已实现收益计入组合。")

    print()
    _print_header("未入选及原因")
    reasons = portfolio.excluded
    if not reasons:
        print("（无）")
    else:
        gate_fail = [item for item in reasons if item[1].startswith("入口闸门未通过")]
        other = [item for item in reasons if not item[1].startswith("入口闸门未通过")]
        if gate_fail:
            print(f"入口闸门未通过 {len(gate_fail)} 个；按首个阻断闸门分类：")
            counts: dict[str, int] = {}
            for val, _reason in gate_fail:
                entry_failed = [gate for gate in val.gates.failed if gate.name in ENTRY_GATES]
                if entry_failed:
                    counts[entry_failed[0].label] = counts.get(entry_failed[0].label, 0) + 1
            for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  - {label}: {count}")
        for val, reason in other[:10]:
            print(f"  - {val.scenario_id}: {reason}")

    print()
    _print_header("预算敏感性")
    for point in appraisal.sensitivity.points:
        added = f" 新增 {'、'.join(point.added)}" if point.added else ""
        removed = f" 移出 {'、'.join(point.removed)}" if point.removed else ""
        print(
            f"预算 {_money(point.budget):>8}: 价值 {_money(point.total_value):>10}，"
            f"成本 {_money(point.total_cost):>8}，场景 {len(point.selected_ids)}{added}{removed}"
        )
    print()
    _print_header("共享容量敏感性")
    capacity_sets = {tuple(point.selected_ids) for point in appraisal.sensitivity.capacity_points}
    for multiplier, point in zip(("0.75x", "1.0x", "1.25x"), appraisal.sensitivity.capacity_points):
        added = f" 新增 {'、'.join(point.added)}" if point.added else ""
        removed = f" 移出 {'、'.join(point.removed)}" if point.removed else ""
        print(
            f"容量 {multiplier:>5}: 价值 {_money(point.total_value):>10}，"
            f"场景 {len(point.selected_ids)}{added}{removed}"
        )
    if len(capacity_sets) == 1:
        print("（容量在 ±25% 内变化不改变入选集合：当前约束由预算而非共享容量绑定。）")

    print()
    _print_header("资料问题")
    if not appraisal.issues:
        print("（无）")
    else:
        by_code: dict[str, list[str]] = {}
        for issue in appraisal.issues:
            by_code.setdefault(issue.code, []).append(issue.scenario_id)
        for code, ids in sorted(by_code.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            sample = "、".join(sorted(set(ids))[:5])
            more = f" 等 {len(ids)} 个场景" if len(ids) > 5 else f"（{len(ids)} 条）"
            print(f"  - {code}: {sample}{more}")


def cmd_show(args: argparse.Namespace) -> int:
    try:
        appraisal = _build(args)
    except (PolicyError, DataLoadError, RegistryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    sid = args.scenario_id
    if sid not in appraisal.valuations:
        print(f"错误：未登记的场景 {sid}", file=sys.stderr)
        return 2
    val = appraisal.valuations[sid]
    evidence = appraisal.evidence[sid]
    scenario = appraisal.dataset.scenarios[sid]
    _print_header(f"场景 {sid}")
    print(f"部门 {val.department} · 流程 {val.process} · 验收指标 {val.target_kpi} · 业务负责人状态 {val.owner_state}")
    print(f"月业务量 {scenario.monthly_volume} · 月基线工时 {scenario.baseline_hours} · 错误率 {scenario.error_rate}")
    print()
    print(f"证据等级：{val.tier_label}（置信度 {val.evidence_confidence}）")
    if evidence.baseline_unit_hours is not None:
        print(f"基线单位工时：{evidence.baseline_unit_hours:.5f} 小时/件")
    if evidence.observed_unit_hours_conservative is not None:
        print(
            f"观察单位工时：最有利 {evidence.observed_unit_hours_best:.5f}，"
            f"最保守 {evidence.observed_unit_hours_conservative:.5f}"
        )
    if evidence.observed_delta_conservative is not None:
        print(
            f"相对基线变化：最保守 {_pct(evidence.observed_delta_conservative)}，"
            f"最有利 {_pct(evidence.observed_delta_best)}（负值表示工时上升）"
        )
    print(f"已观察年值：{_money(val.observed_annual_value)}（{'可计入组合' if val.observed_monetizable else '不可计入组合'}）")
    if not val.observed_monetizable and val.observed_annual_value_indicative != 0:
        print(f"  方向性外推（仅供参考）：≈{_money(val.observed_annual_value_indicative)}，按试点单位工时乘全量月度业务量估算，口径不可比。")
    for note in val.evidence_notes:
        print(f"  · {note}")
    if evidence.flags:
        labels = [FLAG_LABELS.get(flag, flag) for flag in evidence.flags]
        print("证据标记：" + "；".join(labels))
    print()
    print("试点测量：")
    if not evidence.observations:
        print("  （无）")
    for obs in evidence.observations:
        state = ",".join(obs.measurement_states)
        print(
            f"  {obs.period} {obs.source_version} [{state}] "
            f"量 {obs.volume_low}~{obs.volume_high}，单位工时 {obs.unit_hours_low:.5f}~{obs.unit_hours_high:.5f}"
            + ("" if obs.consistent else "  ← 多条测量矛盾")
        )
    print()
    print("闸门：")
    for gate in val.gates.results:
        print(f"  [{'通过' if gate.passed else '未通过'}] {gate.label}：{gate.detail}")
    print()
    print("价值口径：")
    print(f"  基线月成本 {_money(val.baseline_monthly_cost)} · 假设折减 {_pct(val.assumed_reduction)}")
    factor_note = ""
    if val.assumption_contradicted_by_observation:
        factor_note = "，另因观察方向相反 ×0.5"
    print(f"  假设年值 {_money(val.assumption_annual_value)} · 风险调整系数 "
          f"{val.evidence_confidence}×{val.governance_factor}×{val.owner_factor}×{val.readiness_factor}{factor_note}")
    print(f"  风险调整年值 {_money(val.risk_adjusted_annual_value)}")
    first, total = funding_request(sid, appraisal.dataset)
    print(f"  启动未释放 {_money(first)} · 全部未释放 {_money(total)}")
    record = appraisal.registry_records.get(sid)
    if record and record.acceptance:
        acc = record.acceptance
        print()
        print("验收口径：")
        print(f"  {acc.metric} {acc.direction} {acc.threshold} {acc.unit}（{acc.measured_from} 起，{acc.confirmed_by} 于 {acc.confirmed_on}）")
    outcome = appraisal.reviews[sid]
    print()
    print(f"结果回看：{outcome.verdict_label} — {outcome.rationale}")
    return 0


def _acceptance_from_args(args: argparse.Namespace) -> Acceptance | None:
    if args.metric is None:
        return None
    if args.threshold is None or args.unit is None or args.measured_from is None:
        raise RegistryError("设置验收口径需要同时提供 --threshold、--unit 与 --measured-from")
    try:
        threshold = Decimal(str(args.threshold))
    except InvalidOperation as exc:
        raise RegistryError(f"阈值不是合法数值: {args.threshold!r}") from exc
    return Acceptance(
        metric=args.metric,
        direction=args.direction,
        threshold=threshold,
        unit=args.unit,
        measured_from=args.measured_from,
        confirmed_by=args.actor,
        confirmed_on=date.today(),
    )


def cmd_register(args: argparse.Namespace) -> int:
    try:
        appraisal = _build(args)
    except (PolicyError, DataLoadError, RegistryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    sid = args.scenario_id
    if sid not in appraisal.valuations:
        print(f"错误：未登记的场景 {sid}", file=sys.stderr)
        return 2
    registry = Registry(args.registry)
    records = registry.load() if registry.exists() else {}
    try:
        acceptance = _acceptance_from_args(args)
        if acceptance is None:
            raise RegistryError("register 需要至少一项验收口径（--metric 等）")
        val = appraisal.valuations[sid]
        registry.record(
            records,
            action="register" if sid not in appraisal.registry_records else "revise",
            scenario_id=sid,
            rationale=args.rationale,
            actor=args.actor,
            stage="design",
            policy_version=appraisal.policy.policy_version,
            evidence_tier=val.evidence_tier,
            acceptance=acceptance,
            snapshot={
                "risk_adjusted_annual_value": val.risk_adjusted_annual_value,
                "startup_funding": val.startup_funding,
                "value_cost_ratio": val.value_cost_ratio,
            },
        )
        registry.save(records)
    except RegistryError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"已登记 {sid} 验收口径：{acceptance.metric} {acceptance.direction} {acceptance.threshold} {acceptance.unit}")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    try:
        appraisal = _build(args)
    except (PolicyError, DataLoadError, RegistryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    sid = args.scenario_id
    if sid not in appraisal.valuations:
        print(f"错误：未登记的场景 {sid}", file=sys.stderr)
        return 2
    registry = Registry(args.registry)
    records = registry.load() if registry.exists() else {}
    record = records.get(sid)
    val = appraisal.valuations[sid]
    action = args.action
    try:
        amount = None
        if action == "release":
            if record is None or record.acceptance is None:
                raise RegistryError("释放资金前必须先登记验收口径，否则无法回看是否达标")
            if not val.gates.passed:
                failures = "；".join(f"{gate.label}" for gate in val.gates.failed)
                raise RegistryError(f"闸门未通过，不能释放资金：{failures}")
            first, _total = funding_request(sid, appraisal.dataset)
            if first <= 0:
                raise RegistryError("该场景没有可释放的未释放资金")
            release_stage = next_outstanding_stage(sid, appraisal.dataset)
            if release_stage is not None and release_stage.committee_decision == "held":
                raise RegistryError(
                    f"资金阶段 {release_stage.stage_id} 的委员会结论为 held，须先改为 approved 才能释放"
                )
            amount = Decimal(str(args.amount)) if args.amount is not None else first
            if amount <= 0:
                raise RegistryError("该场景没有可释放的未释放资金")
            if amount > first:
                raise RegistryError(
                    f"释放金额 {amount} 超过当前阶段未释放余额 {first}；后续阶段需另行决策"
                )
        elif action == "pause":
            if record is None:
                raise RegistryError(
                    "该场景没有已制定的阶段决策；暂停只适用于已批准并进入执行的阶段"
                )
            _first, total = funding_request(sid, appraisal.dataset)
            releasable, preserved, note = pause_check(
                record,
                required=total,
                released_from_data=sum((stage.released_amount for stage in appraisal.dataset.funding_for(sid)), Decimal("0")),
            )
            amount = Decimal(str(args.amount)) if args.amount is not None else releasable
            if amount <= 0:
                raise RegistryError(
                    "没有可暂停的未释放资金；已释放与已形成合同金额须保留"
                )
            if amount > releasable:
                raise RegistryError(f"暂停金额 {amount} 超过可暂停余额 {releasable}（{note}）")
            print(f"暂停说明：{note}")
        elif action == "advance":
            if record is None or record.acceptance is None:
                raise RegistryError("推进前必须先登记验收口径")
            if not val.gates.passed:
                failures = "；".join(f"{gate.label}" for gate in val.gates.failed)
                raise RegistryError(f"闸门未通过，不能推进：{failures}")
        entry = registry.record(
            records,
            action=action,
            scenario_id=sid,
            rationale=args.rationale,
            actor=args.actor,
            stage=args.stage,
            amount=amount,
            policy_version=appraisal.policy.policy_version,
            evidence_tier=val.evidence_tier,
        )
        registry.save(records)
    except RegistryError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    label = DECISION_KINDS[action]
    extra = f"，金额 {amount}" if amount is not None else ""
    print(f"已记录：{label} {sid}{extra}")
    if action in {"register", "revise"} and record is None:
        print("提示：验收口径已写入 scenarios.json，可用 review 查看判定。")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    try:
        appraisal = _build(args)
    except (PolicyError, DataLoadError, RegistryError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    _print_header("结果回看")
    if not appraisal.registry_records:
        print("尚未登记任何场景的验收口径；所有场景均判定为数据不足。")
        print("使用 register 子命令登记验收指标、方向与阈值。")
    counts: dict[str, int] = {}
    for sid in appraisal.dataset.scenario_order:
        outcome = appraisal.reviews[sid]
        counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
        val = appraisal.valuations[sid]
        record = appraisal.registry_records.get(sid)
        stage = record.current_stage if record else "未登记"
        print(f"{sid} [{outcome.verdict_label}] 阶段={stage} 风险调整年值={_money(val.risk_adjusted_annual_value)}")
        print(f"    {outcome.rationale}")
        if record and record.acceptance:
            acc = record.acceptance
            print(f"    原始验收：{acc.metric} {acc.direction} {acc.threshold} {acc.unit}")
    print()
    print("汇总：" + "，".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retail-agent-portfolio",
        description="零售企业 Agent 场景组合的价值证据评估与阶段拨付管理",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=DEFAULT_DATA, help="CSV 资料目录（默认 data）")
    common.add_argument("--policy", default=None, help="政策 JSON 路径（默认 config/policy.json）")
    common.add_argument("--registry", default=DEFAULT_REGISTRY, help="登记目录（默认 registry）")

    sub = parser.add_subparsers(dest="command", required=True)

    evaluate = sub.add_parser("evaluate", parents=[common], help="评估全部场景并选择组合")
    evaluate.add_argument("--budget", default=None, help="覆盖默认预算")
    evaluate.add_argument("--reduction", default="base", choices=["conservative", "base", "optimistic"])
    evaluate.add_argument("--format", default="text", choices=["text", "json"])
    evaluate.set_defaults(func=cmd_evaluate)

    show = sub.add_parser("show", parents=[common], help="查看单个场景的证据与闸门")
    show.add_argument("scenario_id")
    show.add_argument("--budget", default=None)
    show.add_argument("--reduction", default="base", choices=["conservative", "base", "optimistic"])
    show.set_defaults(func=cmd_show)

    register = sub.add_parser("register", parents=[common], help="登记场景验收口径")
    register.add_argument("scenario_id")
    register.add_argument("--metric", default=None, choices=["unit-hours", "outcome", "total-hours", "volume", "error-rate"])
    register.add_argument("--direction", default="at-most", choices=["at-least", "at-most"])
    register.add_argument("--threshold", default=None)
    register.add_argument("--unit", default=None)
    register.add_argument("--measured-from", default=None)
    register.add_argument("--actor", required=True)
    register.add_argument("--rationale", required=True)
    register.add_argument("--budget", default=None)
    register.add_argument("--reduction", default="base", choices=["conservative", "base", "optimistic"])
    register.set_defaults(func=cmd_register)

    decide = sub.add_parser("decide", parents=[common], help="记录阶段决策")
    decide.add_argument("action", choices=["advance", "hold", "release", "pause", "stop", "note"])
    decide.add_argument("scenario_id")
    decide.add_argument("--actor", required=True)
    decide.add_argument("--rationale", required=True)
    decide.add_argument("--stage", default=None, help="决策所处阶段，如 design/pilot/scale")
    decide.add_argument("--amount", default=None, help="释放或暂停的金额")
    decide.add_argument("--budget", default=None)
    decide.add_argument("--reduction", default="base", choices=["conservative", "base", "optimistic"])
    decide.set_defaults(func=cmd_decide)

    review = sub.add_parser("review", parents=[common], help="按登记的验收口径回看试点结果")
    review.add_argument("--budget", default=None)
    review.set_defaults(func=cmd_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
