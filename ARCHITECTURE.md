# SupplyAgent 当前架构

> **文档契约** · 类型：契约层 · 读取：Feature 开始时按 `## ` 章节标题定点读，禁止通读
> 更新：模块划分、职责归属、依赖方向或不变量变更时由 Claude 写入
> 独占：模块职责与数据所有权、依赖方向、不变量摘要、物理结构边界、修改导航
> 不收录：决策理由（见 `DECISIONS.md`）、需求条款（见 `productinfo.md`）、当前状态（见 `PROGRESS.md`）、命令实现（见 `Makefile`）

本文是当前有效系统架构的顶层事实来源。它描述稳定职责、数据所有权、关键语义变化、约束和依赖方向；产品目标不等于当前实现，项目尚无代码，具体实现细节以后续代码为准，本文档届时随之更新。

## 请求链路

```text
用户 UI（交互式入口）
  → API：鉴权与路由分发
  → Supervisor：意图路由、复杂度判断、加载/保存结构化对话 state
      → 简单路径：Query Agent（NL2SQL / MCP 外部查询）→ Summary Agent
      → 分析路径：Query → Detail Agent（信息收集）→ Research Agent（推理判断）
                        → Summary Agent → Action Agent
  → PermissionDecision：写操作前的人工审批门
  → 执行（工单创建 / 通知发送 / 采购草稿等）
  → Persistence：Run / ToolCall / PermissionDecision 落库

cron Scheduler（自主监控入口，与上方交互式入口共用同一条权限链）
  → Monitor：计算库存周转率 / 缺货率 / 物流异常等指标（纯程序，不含 LLM）
  → 越过阈值 → TriggerEvent → 进入 Supervisor，走与用户交互相同的路由与审批流程
```

## 一级逻辑模块

“尚未成立”表示职责已在设计中明确，但尚无对应代码；“计划代码落点”是设计阶段的目录规划，不代表已存在，不得据此虚构已完成的实现。

「必须维护的 invariants」列每条为一句话摘要，实现级细节写在对应 `src/*/AGENTS.md`，两者不得互相复述（分工规则见 `CODING_RULES.md`「不变量的双写分工」）。

| 模块 | 状态与计划代码落点 | 职责与数据所有权 | 关键语义变化 | 必须维护的 invariants | 主要依赖 |
|---|---|---|---|---|---|
| API | 尚未成立：计划 `src/api/` | 拥有 HTTP 请求 DTO、路由分发、鉴权中间件；不拥有业务规则或 Agent 编排逻辑 | HTTP 输入 → 已校验请求 → Supervisor 调用 | 鉴权失败必须拒绝而非降级放行；业务推理不进入路由层 | Supervisor |
| Supervisor | 尚未成立：计划 `src/supervisor/` | 拥有意图路由、复杂度判断（简单/分析）、结构化对话 state、worker 调度 | 用户输入 / TriggerEvent → 路由决策 → Worker 调用序列 | 升级到分析路径必须命中显式规则，不由 LLM 每次自行判断；state 落库为结构化对象，不常驻原始对话历史 | API、各 Worker、Persistence |
| Query Agent | 尚未成立：计划 `src/workers/query/` | 拥有 NL2SQL 生成与只读执行、MCP 外部查询调用 | 自然语言 → 结构化查询 → 查询结果 | 仅生成只读 SELECT；schema 暴露范围受限（具体准则待定）；不执行写操作 | Tools（Skill/MCP）、Persistence（只读副本） |
| Detail Agent | 尚未成立：计划 `src/workers/detail/` | 拥有多维度信息收集（供货余量、价格、项目资料、规格书等）的 fan-out 编排与中间产物 | ResearchTask → 并行工具调用结果 | 只做采集编排，不做语义判断；采集失败必须显式标记，不得拼凑摘要 | Tools（Skill/MCP）、采购业务核 |
| Research Agent | 尚未成立：计划 `src/workers/research/` | 拥有对 Detail 采集结果的语义推理与归纳建议（ResearchFindings） | 采集结果 → 结构化发现 + evidence_ref | 数值类对比由采购业务核计算，Research 只判断“哪些差异重要”；不裁定算术正确性 | Detail 输出、采购业务核 |
| Summary Agent | 尚未成立：计划 `src/workers/summary/` | 拥有面向运营人员的结构化业务报告生成 | Research / Query 结果 → 自然语言报告 | 只做展示层归纳，不产生新决策或新数据 | Research / Query 输出 |
| Action Agent | 尚未成立：计划 `src/workers/action/` | 拥有执行指导生成、工单创建 / 通知发送等写操作的发起 | 报告 + 用户确认 → 待审批的写操作请求 | 所有写操作必须先过 PermissionDecision；不得因自动触发绕过审批 | 采购业务核、PermissionDecision、Tools |
| 采购业务核 | 尚未成立：计划 `src/procurement_core/` | 拥有缺口计算、候选选型、报价计算、状态机等确定性业务规则 | 需求 / 库存 / 候选 → 缺口与方案（版本化 + 内容哈希） | 算术与硬规则由程序实现，LLM 不裁定；候选不确定项转人工，不自动批准 | Persistence |
| Monitor | 尚未成立：计划 `src/monitor/` | 拥有库存周转率 / 缺货率 / 物流异常等指标计算、TriggerEvent 生成 | 定时快照 → 指标 →（越阈值时）TriggerEvent | 不含 LLM 调用，纯程序计算；越阈值才生成事件，避免高频空转 | Persistence（metrics_snapshot）、Scheduler |
| Runtime 契约层 | 尚未成立：计划 `src/contracts/` | 拥有 Session / Run / ToolCall / ToolResult / PermissionDecision / RuntimeEvent 等跨层共享类型 | 各层调用 → 统一审计记录 | 各 Agent 间不做自然语言对话，只通过该类型化状态传递 | 无（被所有层依赖） |
| Agent Config | 尚未成立：计划 `config/agents/*.yaml` | 拥有各 worker 的 system prompt、模型选择、工具列表声明 | 配置文件 → Runtime 装配 | 走 Git 版本控制；不与业务阈值混放 | Runtime 契约层 |
| Business Rules | 尚未成立：计划 PostgreSQL `business_rule` 表 | 拥有库存告警阈值等可变业务参数，版本化并审计 | 阈值变更 → 新版本 → Monitor / 采购业务核读取 | 变更必须留痕（谁改的、改前改后值、生效时间）；不与 Agent Config 混放 | Persistence |
| Tools（Skill/MCP） | 尚未成立：计划 `src/tools/` | 拥有内部数据库查询（skill）与外部供应商 / 第三方数据源（MCP）的统一调用封装 | Worker 请求 → 标准化 ToolResult | 内部确定性操作用 skill，不套 MCP 协议开销；跨边界集成走 MCP | Infrastructure |
| Persistence | 尚未成立：计划 `src/persistence/` | 拥有 PostgreSQL（事务 / 审计只插入表）与 Redis（缓存 / 幂等去重 / 分布式锁）的存储 contract | 运行时状态 → 可持久化 / 可恢复记录 | 审计表只插入不更新；Redis 不承担恢复所必需的权威事实 | Infrastructure |
| Infrastructure | 尚未成立：计划 `src/infrastructure/` | 提供 LLM、PostgreSQL、Redis、供应商 API 等可替换外部实现 | 核心端口调用 → Provider 调用 → 规范化结果 | 凭据不进代码和 Trace；外部失败不能伪装成功 | 各核心端口、`.env.example`、`compose.yaml`（待建） |

## 依赖方向

```text
API → Supervisor
Supervisor → Query / Detail / Research / Summary / Action
Supervisor → Persistence（对话 state）
Detail / Research / Action → 采购业务核
Query / Detail / Action → Tools（Skill/MCP） → Infrastructure
所有 Worker / Monitor → Runtime 契约层 → Persistence
Action → PermissionDecision（Runtime 契约层的一部分）→ 执行（写操作）
Monitor（cron 触发）→ TriggerEvent → Supervisor
Agent Config、Business Rules → 被 Supervisor / Workers / Monitor 读取，不反向依赖业务逻辑
```

核心职责依赖端口，不依赖 GLM、特定供应商 API、PostgreSQL 等具体产品；具体 Provider 由 Infrastructure 层适配，替换 Provider 不改变核心数据语义。

## 当前物理结构边界

项目尚未创建代码仓库。上表“计划代码落点”是设计阶段的目录规划，Codex 搭建代码时应按本文档的模块边界创建目录，不得自行发明未在本文档出现的模块划分；如需新增或调整模块边界，先更新本文档再动代码。

数据层已落库，分两层且所有权明确：

- `data/supplychain/domdata/`：**原始层，只读不可变**（`chmod 444` + `MANIFEST.json` 记录 SHA-256）。由用户拥有，任何模块和脚本都不得写入。
- `data/supplychain/normalized/`：**规范化层**，由 `normalize_domdata.py` 从原始层单向产出，是下游唯一的消费入口。含 `bom_line` / `bom_line_candidate` / `bom_line_distributor_sku` 三张长表与项目注册表 `projects.yaml`。

Persistence 模块未来把规范化层灌入 PostgreSQL 时，方向只能是 `原始层 → 规范化层 → PostgreSQL`，不得反向回写，也不得跳过规范化层直读原始 CSV。

## 修改导航

| 修改目标 | 首先阅读 |
|---|---|
| API 鉴权与路由 | 本文 API 行；`src/api/AGENTS.md` 中的接口约束 |
| Supervisor 路由与复杂度判断规则 | 本文 Supervisor 行；`DECISIONS.md` D02、D03 |
| Worker 行为、system prompt、模型选择 | 本文各 Worker 行；`config/agents/`（待建）；`DECISIONS.md` D06、D07 |
| 缺口计算、候选选型、报价等业务规则 | 本文“采购业务核”行；`productinfo.md` §5、§8 |
| 业务阈值（如库存告警线） | `business_rule` 表设计（待建）；`DECISIONS.md` D07 |
| Monitor 层指标计算、时间序列数据 | 本文 Monitor 行；`DECISIONS.md` D03、D11；`data/supplychain/normalized/projects.yaml` |
| 元件/BOM 数据现状与已知缺口 | `data/supplychain/normalized/projects.yaml`；转换逻辑见 `normalize_domdata.py` |
| 工具封装（Skill / MCP 边界） | 本文 Tools 行；`DECISIONS.md` D08 |
| 数据分层（PostgreSQL / Redis 分工） | 本文 Persistence 行；早期数据分层设计（待迁移整理） |
| 鉴权 / 多租户 | `DECISIONS.md` D10（当前仅为架构预留，未实现） |

## 历史与当前事实

本项目当前无历史归档区。设计过程中曾提出又被撤回的范围扩展（如议价 Agent、自动补货预测）不作为当前实现指导；完整决策与取舍见 `DECISIONS.md`。回溯讨论细节需用户明确指定工作笔记或 Git revision。
