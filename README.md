# SupplyAgent

SupplyAgent 是面向采购、计划和工程协同人员的**元件供应风险主动预警系统**。它持续监测在用元件的供应状态，在库存告急或制造商宣布停产时主动发出带证据的告警，并围绕告警提供决策支持。确定性计算、多源取证、模型辅助归纳、人工评审和可恢复执行放在同一个可审计 Run 中。

产品心脏是预警而非问答：用户不必先想到要查什么，系统发现问题并找上门。

## 当前产品闭环

1. 纳入监测对象并设定阈值，由提交 BOM/需求等业务事件或用户请求唤醒；
2. 并发采集多源观察：分销商库存报价（数值型）、制造商官方资料（文档型，带原文定位）；
3. 与阈值和上一状态差分，按去重规则产生或关闭告警——同一情况只告警一次；
4. 围绕告警生成决策支持：多源库存调研、比价建议、官方推荐替代、多候选对比；
5. 信息不足时等待人工，依据变化后重新评审；
6. 经授权后可执行外部动作，并通过幂等键核对结果。

本期不建设通用 NL2SQL、库存周转或物流监控平台、通用供应商情报库、固定多 Agent 角色流水线和自动下单；不出具元件适用性或技术等价结论。

## 架构方向

系统以 Agent Harness 为主体：统一多模型后端、上下文编译、工具注册、Evidence/Artifact 和诊断；以持久化 Runtime 支撑长任务、等待、恢复、重试和副作用幂等。业务执行采用状态驱动工作流，包含 deterministic、tool、model 和 human 四类节点。

完整架构与图见 [ARCHITECTURE.md](ARCHITECTURE.md)，当前决策见 [DECISIONS.md](DECISIONS.md)。

## 当前状态

仓库已有本地开发环境、PostgreSQL/Redis、规范化 BOM 数据、数据库迁移、采购确定性业务核和部分 FastAPI 端点。多模型 ModelBackend、Context Compiler、Evidence Ledger、持久化工作流和 UI 尚待实现。准确状态与当前验证见 [PROGRESS.md](PROGRESS.md)。

## 文档入口

| 内容 | 文件 |
|---|---|
| 产品范围与待定项 | [productinfo.md](productinfo.md) |
| 功能需求与业务规则 | [docs/product/requirements.md](docs/product/requirements.md) |
| 验收用例 | [docs/product/acceptance-cases.md](docs/product/acceptance-cases.md) |
| 术语 | [docs/product/GLOSSARY.md](docs/product/GLOSSARY.md) |
| 工具与 Harness 接口 | [docs/spec/interfaces.md](docs/spec/interfaces.md) |
| Run 状态机 | [docs/spec/state-machine.md](docs/spec/state-machine.md) |
| 数据模型 | [docs/spec/data-model.md](docs/spec/data-model.md) |
| HTTP / UI | [docs/spec/http-api.md](docs/spec/http-api.md)、[docs/spec/ui-contract.md](docs/spec/ui-contract.md) |
| Feature 状态 | [docs/features.json](docs/features.json) |

## 本地命令

以 `Makefile` 为唯一命令入口：

```bash
make help
make status
make check
make services-up
make services-smoke
```

真实模型、外部供应商 API、远程服务器或收费能力需要用户明确授权。
