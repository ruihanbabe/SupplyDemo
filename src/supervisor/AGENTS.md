# Supervisor 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有意图路由、复杂度判断（简单 / 分析）、结构化对话 state（当前 SKU、已确认候选、已执行动作、上一轮结论）、worker 调度。交互式入口与 cron Monitor 的 TriggerEvent 共用同一条路由与审批链路。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Supervisor 行与「请求链路」「依赖方向」、`DECISIONS.md` D02 / D03 / D05；代码与测试待建。

## 不变量与 contract

- 升级到分析路径必须命中显式规则（候选数 > 1 / 缺字段 / 查询异常等），不由 LLM 每次自行判断。
- 对话 state 落库为结构化对象，不把完整对话原文常驻 LLM 上下文。
- TriggerEvent 触发的 Run 与用户交互触发的 Run 走同一条 ToolCall + PermissionDecision 链路。
- Supervisor 只做编排，不承担算术 / 硬规则（归采购业务核）与写操作执行（归 Action）。

## 修改后验证

`Makefile` 与测试待建。建立后运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的三层验证补充路由 / 复杂度判断的场景确认。
