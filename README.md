# 企业 Agent 场景组合的价值证据与阶段拨付

本项目附带的业务记录均为本地合成数据，标识和数值不对应真实个人、企业或交易。运行环境使用 Python 3.11 至 3.14 和标准库，无需额外服务。

它帮助集团数字化团队在有限预算下比较 Agent 候选场景，区分**已观察收益、假设收益和缺少基线**，
在共享实施容量约束下给出试点次序，并支持登记验收口径、释放/暂停资金与结果回看。

```sh
PYTHONPATH=src python3 -m retail_agent_portfolio_evidence evaluate --data data
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Windows PowerShell 可运行 `$env:PYTHONPATH="src"; python -m retail_agent_portfolio_evidence evaluate --data data`。
完整用法、评估口径与资金保留规则见 [`docs/使用说明.md`](docs/使用说明.md)；
`docs/资料契约.md` 说明各数据文件及其字段。

## 代码结构

```text
src/retail_agent_portfolio_evidence/
  models.py      领域模型（Decimal 计价，缺失不折零）
  policy.py      政策参数加载与校验
  loader.py      CSV 加载、结构校验与跨表问题识别
  evidence.py    证据分级、期间换算、矛盾测量处理
  gating.py      6 项闸门与资金需求
  valuation.py   已观察/假设/风险调整三类价值
  portfolio.py   预算+共享容量约束的组合选择与敏感性
  registry.py    追加式决策日志与验收口径登记
  review.py      按原始验收口径的结果回看
  appraisal.py   应用服务与 JSON 视图
  cli.py         命令行界面
config/policy.json  可调假设（小时成本、折减率、置信度、容量）
```

当前示例资料中，试点业务量与月度基线口径不可比且观察单位工时高于基线，
因此报告不会把假设节省计为已实现收益；详见使用说明第三节。
