# F30：v0.5 既有能力兼容审计

> 审计日期：2026-10-09 ｜范围：F01–F29 的仓库现状、Contract 与 `features.json` 状态 ｜结论：旧实现可作为供应链领域资产迁入；不能直接构成多租户 CRM 产品。

> 2026-10-10 架构补充：产品服务端重基线为 Python API/Worker/Harness；Next.js/TypeScript 只作薄前端/BFF，Node 只作无状态 Pi sidecar。本矩阵中原本写作 Next/React 的服务端落点，均以 F31–F42 的修订 Contract 为准；不改变本审计关于旧 Python 资产、F05 `eval_only` 及禁止直连 `simenv` 的结论。

## 结论与硬边界

本仓库不是从零开始：它已有一组以 Python、固定样例、单租户模拟环境为前提的确定性采购能力。v0.5 不复制或删除这些能力，而是把它们放到组织范围服务、SQL Trace 和受控执行边界之后。

以下入口禁止进入产品路径：浏览器或 Pi 直调 `simenv`、本机文件作为 Run 恢复事实、固定样例 Provider 冒充 BYOK、以及仅因 `ready_for_review` 就提交采购或发送消息。模型、API 和前端只能通过 F31、F35、F40、F41 定义的边界访问业务。

`awaiting_acceptance` 是旧 Feature 的历史测试状态，不是 v0.5 产品验收；`done` 仅表示其原 Contract 的工作完成。未执行的命令不在本审计中补跑。F05 有未提交实现和测试、未补跑集成验收，故冻结为 `deferred`、`eval_only`，不计入产品完成度。

## 审计方法与现有证据

- 清单来源：`features.json` v3；实现路径：`src/`、`tests/`；语义来源：各 `contracts/Fxx.md`。
- F01–F23 均存在对应实现模块与离线测试。它们的历史状态和证据保留在 `features.json`；本审计不重新宣称它们已具备 RLS、Worker、真实渠道或 BYOK。
- F24–F29 都是 `not_started`，没有对应 Contract 或实现，不能作为现有产品能力。

## F01–F29 迁移矩阵

| 旧 Feature | 可核验资产 / 当前状态 | v0.5 处置 | 不可直接复用的部分 | 目标 Feature 与复验 |
|---|---|---|---|---|
| F01 工程与 `hw_part` | `src/knowledge/`、`src/persistence/`、`src/infrastructure/`；`done` | 保留为硬件原始层、导入与基础工程 | 不承载组织 CRM 数据；旧 schema 不是产品 schema | F39；`make build-parts`、迁入后的组织服务测试 |
| F02 第二块板 | 原始层与板数据；`done` | 保留为跨板样例与 Eval 留出集 | 不是租户隔离或客户数据 | F39/F38；MANIFEST 与 Eval 数据集检查 |
| F03 模拟环境 | `src/simenv/`、环境快照/写日志；`awaiting_acceptance` | 仅保留为情景、回归与 Eval 后端 | 模型、浏览器、API 不得直调；模拟状态不是产品事实 | F39；服务边界与固定情景回放 |
| F04 情景与异常 | `not_started` | 作为 Eval 情景能力重估，不按旧方案单独实现 | 未实现能力不能计入范围 | F39/F38；情景 Contract 后的回放 |
| F05 模拟人工 | `src/humans/`、`config/humans.yaml`、`tests/test_humans.py`；未提交、未完整验收 | 冻结为 `eval_only` | 不是人工审批、渠道、Pi 工具或产品 API | F39；仅作测试后端，默认关闭 |
| F06 事件与 Artifact | `src/session/`；`awaiting_acceptance` | 抽取事件、Artifact、依赖与回放语义 | 旧单运行链不等于组织级 SQL Trace/SSE | F40；组织/Run/actor、Event-ID、脱敏测试 |
| F07 工具代理 / MCP | `src/tools/`；`awaiting_acceptance` | 保留受控工具、缓存、配额、留档和返回校验思路 | 旧 Server 不得绕过组织 scope、Gateway 或外部授权 | F41；工具白名单和跨组织拒绝 |
| F08 模型适配 | `src/models/`；`awaiting_acceptance` | 保留 Schema 校验、降级和输出审计策略 | 固定样例模型不等于组织 BYOK 或产品执行循环 | F34/F42；Gateway 与 Pi 协议测试 |
| F09 Runtime | `src/runtime/`；`awaiting_acceptance` | 保留预算、循环检测、调度语义 | 内存/模拟时钟不等于 SQL queue、lease、reaper 或恢复 | F35/F42；claim/retry/cancel/recover |
| F10 案子管理 | `src/cases/`；`awaiting_acceptance` | 迁为供应链 Case 服务 | 无 `organization_id`、客户关联与 RLS | F39/F32；范围查询与关联权限 |
| F11 权限 | `src/permissions/`；`awaiting_acceptance` | 保留风险级、能力令牌、出站约束和脱敏 | 旧 token 不取代成员身份、RLS 或组织审计 | F31/F41；RLS 与令牌双重拒绝 |
| F12 证据门控 / 审批 | `src/gate/`；`awaiting_acceptance` | 保留证据检查、内容哈希绑定与提交前重校验 | 旧人工事件不是产品审批收件箱或授权生命周期 | F41/F37；Proposal、短期令牌、二次校验 |
| F13 污点追踪 | `src/taint/`；`awaiting_acceptance` | 保留外部文本隔离与汇点检查 | 未覆盖 CRM/渠道消息和新 Artifact 模型 | F41/F36；渠道与知识文本污点 |
| F14 反馈 / 升级 | `src/feedback/`；`awaiting_acceptance` | 迁为 Run 重规划与人工恢复策略 | 不取代真实审批、通知或渠道投递 | F35/F37；恢复状态机 |
| F15 恢复 / 幂等 | `src/resilience/`；`awaiting_acceptance` | 保留意图/完成、幂等、重试、熔断原则 | 旧进程恢复不是 SQL Job lease/reaper | F35/F41；崩溃、重复投递、执行账本 |
| F16 上下文组装 | `src/context/`；`awaiting_acceptance` | 保留裁剪、引用块与折叠策略 | 不可给 Pi 传本机 session 或跨组织内容 | F42/F36；按组织、角色和工具白名单快照 |
| F17 计量 | `src/observability/`；`awaiting_acceptance` | 保留成本、工具使用、配额计量概念 | 旧计量不等于 Gateway 账目或 SQL Trace | F34/F40；组织计量与审计 |
| F18 注入 / 回放 / 分叉 | `src/replay/`；`awaiting_acceptance` | 保留故障注入、回放、分叉方法 | 旧运行清单不是产品 Trace 的唯一事实 | F40/F38；SQL Trace 回放和 Eval |
| F19 文档知识库 A | `src/datasheets/`；`awaiting_acceptance` | 迁为私有文档、版本与引用能力基础 | 固定样例、无组织 ACL，不等于企业 Wiki/RAG | F36；ACL、版本、citation |
| F20 知识库 B / Skills | `src/knowledge_base/`、`skills/`；`awaiting_acceptance` | 保留术语、替代关系、失效与人工确认原则 | 无组织权限；旧 Skills 不自动授予工具 | F36；组织记忆、撤销、引用 |
| F21 确定性业务组件 | `src/business/`；`awaiting_acceptance` | 保留影响分析、比价、策略、采购草稿的不变量 | 模型不得生成数值或直接执行采购 | F39/F41；服务命令和 Proposal |
| F22 编排者 | `src/orchestrator/`；`awaiting_acceptance` | 保留任务图、模板兜底、受限工具集策略 | Python 主循环不等于 Pi 执行 Runtime | F42；Pi 输入、事件映射、并发限制 |
| F23 子 Agent / LLM 函数 | `src/agents/`；`awaiting_acceptance` | 保留隔离、引用验证和确定性后处理策略 | 不保留旧执行器或固定模型配置 | F42/F36；Specialist 工具范围与引用 |
| F24 只读问答 | `not_started` | 按新知识与 Specialist 设计重开 | 无实现可复用 | F36/F33；引用问答验收 |
| F25 Web 人工节点 | `not_started` | 由产品审批与 Workbench 取代 | 不沿用旧单租户 Web 假设 | F37/F33；审批收件箱与权限 |
| F26 Web 时间线 / 配额 | `not_started` | 由 Workbench 与 SQL Trace 投影取代 | 无实现可复用 | F33/F40；SSE 断线回放与配额 |
| F27 Eval 框架 | `not_started` | 吸收至统一 Trace/Eval | 无实现可复用 | F38；回放、隔离、过程评分 |
| F28 对照 / 消融 | `not_started` | 配合 F38 按配置重建 | 不可声称已有消融体系 | F38；配置化实验运行 |
| F29 Compose 交付 | `not_started` | 按 Web/API/Worker/DB/Redis 新拓扑实现 | 旧单体运行方式不适用 | F38；Compose 启动与演示回放 |

## 参考架构能力的唯一落点

| 目标能力 | 当前可用遗产 | 缺口 | 唯一后续落点 |
|---|---|---|---|
| CRM / 客户 / 商机 / Kanban | 无 | 领域模型、权限、Proposal | F32 |
| API、组织、RBAC、RLS | F11 的局部令牌策略 | 身份、组织字段、RLS、服务端 API | F31 |
| SQL Worker / 可恢复队列 | F09/F15 的调度与恢复原则 | Job 表、Worker、claim/lease/reaper/retry | F35 |
| Pi 执行拓扑 | F08/F16/F22/F23 的策略 | JSONL Adapter、SQL checkpoint、Pi 生命周期 | F42（基于 F35） |
| BYOK / 模型网关 | F08/F17 | 凭证隔离、Provider、预算与成本账目 | F34 |
| Specialist / Wiki / RAG | F19/F20/F23 | 组织 ACL、版本、引用、长期记忆边界 | F36 |
| Proposal / 审批 / 渠道副作用 | F07/F11/F12/F13/F15/F21 | 统一执行账本、渠道、发送前重校验 | F41，渠道界面由 F37 |
| Workbench / SSE | F06/F17 的事件与计量语义 | Next/React 薄前端、Python API 的 Event-ID 投影、审批收件箱 | F33（依赖 F40） |
| SQL Trace / Eval / 交付 | F06/F17/F18 与 `simenv` | 组织级 Trace、Eval、Compose | F40、F38 |

## 后续唯一顺序

F30 用户验收后，只启动 F31。F31 完成前，不启动 F39、F40、Pi、渠道或前端；它们会缺少组织上下文、权限与 RLS 的可靠边界。F05 保持冻结，不是阻塞项。
