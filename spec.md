# spec.md：SupplyAgent AI Native CRM 与供应链处置系统

> 版本：v0.6（2026-10-23 整理）
>
> 本版按用户要求重基线：参考架构图中的功能全部保留为产品目标；现有 SupplyAgent 的采购、证据、审批和可复现 Harness 能力作为其中的供应链业务域。图是功能参考，不代表取得其源码、数据或品牌资产。

## 0. 定位、范围与完成定义

### 0.1 产品定位

SupplyAgent 是一个多租户的 **AI Native CRM 与供应链处置系统**。它同时管理客户、商机、会话、任务与采购异常案子；Agent 在有边界的运行时内诊断、检索、提出方案、创建可控草稿，并把改变外部世界的动作交给人工决策和渠道投递链路。

采购异常处置不是被删除的旧功能，而是首个深业务工作流：缺料、替代料、供应商报价、采购草稿、审批、提交、到货跟踪和计划员协作。CRM 负责把客户/商机/渠道上下文与这些工作流连起来。

### 0.2 本期完整目标

“完整”是指下表每项都有 Contract、可运行实现和验收证据；不能把静态 Demo、未接线的页面或模拟成功视作完成。

| 参考图能力 | 本系统的保留方式 |
|---|---|
| CRM 与 Agent 工作台 | 客户、商机、Kanban、任务、案子、运行时间线、React 工作台 |
| API 与租户身份 | Route Handlers、组织/成员/RBAC、scope/role、RLS、SSE 回放 |
| 服务端信任边界 | Next 薄展示/BFF、Python 业务层、SQL 持久化、独立 Worker/cron；模型和页面都不直连数据库 |
| 模型网关 / BYOK | 组织凭证隔离、Provider Adapter、模型调用、预算/成本/审计 |
| Agent Harness | 任务图、工具循环、上下文重建、取消/恢复、提案策略、父子 Agent 汇总 |
| Durable Runtime | Mission/Run/Job、角色执行记录、Trace、claim/lease/reaper/retry |
| 事实、记忆与恢复 | 版本化业务事实、组织策略、checkpoint、运行恢复时重读业务边界 |
| Specialist 协作 | 客户事实、商机、采购、供应链、知识检索、Verifier/Explainer；独立 review 可并行 |
| 企业知识 / Wiki / RAG | 私有文件、版本、权限、citations、全文检索与可选向量检索 |
| MCP 工具与受控写 | 只读业务服务、受控变更/提案、模型不直访 SQL、所有副作用可追溯 |
| 人工决策与渠道投递 | 动作分级、审批/拒绝/恢复、发送前再校验、WhatsApp 与飞书适配器 |
| SQL Trace、Eval、部署 | Event-ID SSE、Trace/Artifact、Eval、容器化、回放、可验证交付 |

### 0.3 不可突破的边界

| 编号 | 规则 |
|---|---|
| G1 | 密钥、BYOK、渠道令牌永不进入模型上下文、事件文本或前端；仅服务端凭证库可读。 |
| G2 | 一切外部副作用（采购提交、客户字段写入、消息发送）都必须经动作分级、提案、人工授权令牌与提交前重校验。 |
| G3 | 模型不能直连 SQL、RLS 绕过接口或渠道 Provider；只能调用受控工具。 |
| G4 | 数量、金额、库存、交期、评分与权限结论由确定性代码或已验证来源提供；模型不能生成后直接落库。 |
| G5 | 实际外部调用、付费模型调用、批量检索和渠道发送须获用户/组织授权；离线测试用固定样例或回放。 |
| G6 | 外部文本作为数据，做污点追踪和引用隔离；不可把工具结果当系统指令执行。 |
| G7 | 业务策略、配额、风险级别、重试和渠道路由全部配置化，不硬编码。 |
| G8 | Agent 不排产、不直接改生产计划或工单；仅提交影响分析给计划员。计划员的独立决定作为新事实进入系统。 |

### 0.4 文档与重基线规则

- `spec.md` 是目标需求的唯一权威来源；`features.json` 是实现清单和状态的唯一权威来源；每个 `contracts/Fxx.md` 是单项边界与验收证据。
- 产品范围、验收和依赖只由 F30 及以后的 Feature Contract 定义。遗留 Alembic 迁移仅为历史数据库维护记录，不是当前产品能力或复用依据。
- Feature 是否可开工只看 `features.json` 和对应 Contract。一次只允许一个 Agentic Feature 处于 `in_progress`，其余能力即使已有 SQL 或历史代码，也必须按实际状态标为草案、延后或等待验收。

业务规则唯一正文见 [business-logic.md](business-logic.md)，跨模块数据约定见 [data-contract.md](data-contract.md)。本文件保留目标与架构，实施状态由 features.json 管理。

## 1. 产品对象、角色与数据边界

### 1.1 多租户对象

组织是隔离单元。组织下有成员、角色、客户、联系人、商机、会话、供应链案子、采购草稿、运行、知识文档、渠道连接与审计事件。每条业务记录必须携带 `organization_id`，所有服务查询必须带组织上下文，PostgreSQL RLS 是最后一道隔离。

本期角色至少为：组织管理员、销售/客户经理、采购员、审批主管、硬件工程师、计划员、只读审计员、维护者。角色给出默认能力；运行时实际权限以短期能力令牌、对象 scope 和动作风险级别共同决定。

### 1.2 两类业务工作流

1. **CRM 协同**：渠道事件或工作台操作建立客户/商机上下文，Agent 读取事实和知识，产出跟进提案、客户字段更新提案或回复草稿；人工批准后才更新或发送。
2. **供应链处置**：环境或人工事件建立采购异常案子，诊断根因、计算影响、检索/验证替代方案、比价、生成采购草稿、审批、提交与跟踪。该链路保留现有 SupplyAgent 的严格证据门控与 G8 边界。

二者可以相互关联，但不能共享未授权数据：例如销售人员只可看到自己 scope 内的客户和与其关联的汇总采购风险，不可查看供应商凭证或全量 BOM。

## 2. 总体架构与数据流

```text
Next.js Web 工作台 / WhatsApp / 飞书事件
  -> Python API 身份、组织上下文、RBAC、请求校验
  -> 创建 Mission/Run 与 SQL Job（仅服务端）
  -> Durable Runtime Worker claim/lease/retry
  -> Python Product Harness（上下文、策略、工具、证据、编排）
  -> AgentEnginePort -> PiRuntimeAdapter -> 受控 Node Pi sidecar
  -> Model Gateway / Specialist / Tool Proxy
  -> MCP 工具代理（只读服务 / 受控提案）
  -> PostgreSQL + RLS + Redis + 私有文件/知识库
  -> 人工审批、发送前重校验、渠道 Adapter
  -> Event Log / SSE / Trace / Eval / 审计
```

### 2.1 API 与工作台

当前交付入口与进程运行以 [Model Gateway 与 Worker 运行说明](docs/design/model-gateway-worker-contract.md) 为准；本节及 §2.8 的长期渠道目标保留后续实施。

前端采用 Next.js / React 的工作台，至少提供客户、商机、供应链案子、Kanban、运行时间线、审批收件箱、知识文档和配额视图。Next 是薄展示层：负责页面渲染、浏览器会话承载和同源 BFF 转发，不承载领域规则、SQL Repository、Worker、Gateway 或 Agent Runtime。所有产品业务 HTTP 与 SSE 接口由 Python API 提供；API 在创建 Run/Mission 后只入队，不在 HTTP 请求内执行 Agent turn。

Python API 是身份、组织上下文、RBAC、请求 schema、领域服务与 SQL 事务的唯一服务端实现。它逐请求验证 Supabase Auth access token，忽略客户端声称的 actor、角色或组织；选中组织仅可作为提示，Python 必须在同一数据库事务内重新验证成员关系并调用 `app.set_request_context`。Next 不签发可被 Python 信任的身份断言，也不持有 PostgreSQL service role。

浏览器只调用 Next 的同源 BFF 路径；BFF 只转发允许的方法、路径、会话 cookie/authorization 和关联 ID 到 Python API，不能改变身份、组织、scope、请求体或权限结果。Python SSE 是 Event Log 的唯一读取投影；Next 仅流式转发，不缓存、不重排也不合成事件。这样保留当前前端构建与浏览器边界检查，同时把产品逻辑集中到 Python。

### 2.2 Durable Runtime 只管理产品运行生命周期

当前 SQL 权威记录为 runtime_mission/runtime_run/runtime_job 与 trace_run/trace_event/trace_artifact，F45 追加私有 attempt/Observation；通用 session、Context 状态、审批和发送账本是后续契约，不能当成已实现表。Worker 对 Job 使用 claim、lease、reaper 和 retry；重试、取消、恢复和幂等键均由 Python 实现。Runtime 从最新 checkpoint 和当前业务事实重建上下文，不信任进程内状态。

Python Worker 领取 Agent Job 后调用 F42 定义的 `AgentEnginePort`。F35 负责何时开始、停止、重试和恢复一个 turn，F42 负责 turn 内的模型消息、工具调用和终止语义。这个分界让 Runtime 可以替换执行引擎，也避免把 Pi 的进程状态误当成产品状态。

### 2.3 Product Harness 拥有业务控制，Pi 只执行 Agent turn

当前身份、实际输入与持久记录对应见 [任务执行数据契约](data-contract.md)，已实现与未决设计分别标记。

Python Product Harness 负责组装 `ContextBundle`、任务和工具能力，执行组织权限与策略检查，路由模型请求，调用只读工具或创建 Proposal，保存证据与 Trace，并决定父子任务如何汇总。它通过稳定的 `AgentEnginePort` 调用执行引擎，不能依赖某个 Pi 类或 Node 内部对象。

Pi Agent Core 作为首选 `AgentEnginePort` 实现，保留上游的消息循环、模型流式事件、工具调用与工具结果回送、取消和 turn 终止语义。Python `PiRuntimeAdapter` 管理受控 Node 子进程，双方通过版本化 JSONL 交换命令与事件。Node 不访问 PostgreSQL，不保存产品 checkpoint，不包含组织策略、工具授权或副作用实现，也不拥有 Provider、渠道和数据库凭证。

| 模块 | 必须实现的职责 | 不得拥有的职责 |
|---|---|---|
| Python Product Harness | ContextBundle、任务图、工具注册与授权、模型路由、证据、Artifact、Proposal 策略、Specialist 编排 | Pi 内部会话对象、上游私有实现细节 |
| AgentEnginePort | 启动 turn、投递模型/工具结果、流式事件、取消、错误分类、完成结果 | 租户、数据库、审批和渠道语义 |
| Pi Node sidecar | 调用锁定版本的 Pi Agent Core，并把上游事件转换为 JSONL 协议 | SQL、长期记忆、业务规则、真实副作用和持久凭证 |
| SQL Durable Runtime | Run/Job、claim/lease/retry、checkpoint、恢复和幂等 | turn 内推理和工具选择 |

该拓扑不采用 Pi Coding Agent 的 shell、文件编辑、终端、桌面会话、扩展加载和本地 JSONL 恢复能力。以后若确需其中某项，必须建立单独 Contract、权限模型、消融开关和验收证据。若上游 Pi 只能通过维护私有 fork 才能满足端口，系统保留 `AgentEnginePort` 并评估替代引擎，不把 fork 变成产品前提。

### 2.4 Model Gateway / BYOK

模型调用必须经过 Gateway。Gateway 按组织选择 Provider/模型，读取隔离凭证，执行参数/Schema 校验、限流、预算预检、成本计量、审计与可选降级。它不等于全局承诺：每次调用只做组织级配额检查；具体预算策略写在配置和 Contract 中。

### 2.5 Agent 与 Specialist

业务场景与固定角色/工具分工见 business-logic BL01/BL03。Harness 负责有界编排，Pi 不拥有领域状态；具体交接与结果质量见 data-contract DC03/DC05/DC06/DC08。动态 Planner/冲突裁决是后续目标。

### 2.6 知识、记忆与证据

知识提供正式版本化资料；长期记忆提供有来源/时间/范围的偏好、历史决策、任务过程与经确认经验；正式业务状态由领域服务维护。业务规则正文见 [BL08/BL09/BL13](business-logic.md)，身份/版本/加工/删除与请求接受约定见 [DC18–DC21](data-contract.md#dc18-会话消息工作状态与提交水位f52-目标)。

F48 主选 Mem0 OSS，复用唯一有效记忆语义检索，默认发布带来源的通用过程摘要，稳定命题抽取按需使用；PostgreSQL 保存准确摘要/确认记忆版本、来源与当前权限/生命周期，Mem0 是可重建投影。候选加工、确认发布、异步对账、软忘记/硬删除和离线巩固由 Python 管理。后端不能满足隔离/擦除等验收时按同业务样本重评，不并存多套有效索引。

F52 管 Conversation、近期完整消息单元、Working State/checkpoint、每次模型请求的 ContextSnapshot、分区预算、渐进读取与 Compaction。Python 决定可发送投影，Pi 保留消息/工具循环并在每次模型请求前交接准备；Gateway 最终校验并记录实际请求。commit 代表来源/状态及待加工水位持久化，不等于长期记忆已可检索；Compaction 摘要不自动成为确认记忆。

开发顺序与验收分别见 [F51](contracts/F51.md) → [F52](contracts/F52.md) → [F48](contracts/F48.md)。F52 可在空 MemoryPort 下独立使用；F48 对接其来源提交与注入 Interface，无反向依赖。模块接口见 [Memory](src/product_api/MEMORY_RUNTIME.md)、[Context](src/product_api/CONTEXT_RUNTIME.md)，传输见 [Pi Context 协议](pi_sidecar/CONTEXT_PROTOCOL.md)。当前仍只有已有最小 Harness、角色启动快照与旧 F36 记忆生命周期，上述新能力尚未实现。

### 2.7 工具、提案与副作用

工具分为 R0 只读、R1 可补偿写/提案、R2 外部不可逆副作用。模型只拿到与当前执行者和 scope 匹配的工具集合。

- CRM 读取：客户、联系人、商机、会话、组织记忆、知识检索。
- 供应链读取：库存、采购单、运单、工单、报价、文档和影响分析。
- 受控变更：客户字段更新、商机更新、采购草稿、发送草稿，均先产生 Proposal。
- 外部副作用：采购提交、WhatsApp/飞书发送必须由审批记录签发的令牌授权，并在提交前重校验权限、service revision、opt-out、草稿哈希和 send ledger。

F51 已实现待验收的工具机制采用分离的 ToolRegistry、ToolRuntime 和 ToolScheduler。Registry 管声明、精确别名、可用性与角色绑定；Runtime 管准备、授权/范围重验、领域执行及持久结果；Scheduler 管完整批次的顺序屏障、连续兼容组、资源准入、取消与结果归位。Mission 仍管理业务依赖、必需分支及汇总，不把这些职责下放工具调度。

Python 是工具执行调度的唯一所有者；Pi 提交完整工具批次并按原身份消费结果，保留消息循环。声明中的副作用、并发兼容性和资源约束相互独立，调度不推导业务依赖图或一致快照。当前实施范围仍限 R0；R1/R2 与 MCP 的目标不因新机制而自动启用。业务策略见 [BL12](business-logic.md#bl12-工具绑定执行与失败策略f51-目标)，数据见 [DC15–DC17](data-contract.md#dc15-工具定义绑定与执行身份f51-目标)；接口与算法见 [Python 模块设计](src/product_api/TOOL_RUNTIME.md)，Pi 批次及版本协议见 [sidecar 文档](pi_sidecar/TOOL_PROTOCOL.md)。实施状态与分阶段验收见 [F51](contracts/F51.md)。

### 2.8 人工决策与渠道

人工可批准、拒绝、要求恢复/重规划或编辑受权限允许的草稿。审批只是针对当前内容哈希的短期授权，不是永久运行许可。渠道层实现 WhatsApp adapter 和飞书内部协作 adapter：前者可将客户会话接入 Workbench，后者仅作内部事实/通知，均不得绕过审批或直接授予写权限。

## 3. 供应链领域的保留与改造

供应链能力必须在组织上下文、Proposal 模型、统一事件日志和 Worker 生命周期内重新实现。测试夹具或本地样例都不能成为 API、模型工具或绕过 RLS 的入口。

采购流程的状态至少为：发现 → 诊断 → 影响分析 → 方案/报价 → 草稿 → 门控 → 审批等待 → 提交前重校验 → 提交/跟踪 → 关闭或升级。供应链和 CRM 都复用同一 Run、Artifact、Approval、Proposal、Trace 和 Audit 模型。

### 3.1 供应链领域边界

供应链数据只通过组织范围的 Python 服务、SQL/RLS 与受控工具进入产品路径。模拟器、本地文件和旧数据库结构都不得作为产品 API、模型工具或 Run 恢复事实。评估、数据导入和外部渠道需要各自的已确认 Contract 与可执行验收。

## 4. 可观测性、评估与运行

每个 Run 记录模型/工具/人工/渠道事件、Artifact 版本、输入输出摘要、关联 Event-ID、成本、使用量、错误与重试。SSE 可按 Event-ID 断线回放；SQL Trace 是审计事实来源。可选 Langfuse 只接收脱敏 span/score/OTLP，不替代 SQL Trace。

Eval 覆盖结果正确性、过程合规性、成本/时延、恢复与重复投递、跨组织隔离、渠道发送前重校验。必须支持固定样例、数据回放、全回放、故障注入、分叉和消融。部署目标是 Docker Compose 的 Web、API/Worker、PostgreSQL、Redis 与所需本地存储；生产化扩展不改变核心边界。

## 5. Feature 划分与顺序

F30 确立 v0.6 产品基线；后续 Feature 只依赖已定义的产品边界和可执行验收，不依赖已清理的旧实现。

| 顺序 | Feature | 目标 |
|---|---|---|
| 1 | F30 | 现有能力兼容审计，形成迁移清单，禁止未经核验宣称已实现。 |
| 2 | F31 | 多租户身份、组织隔离、RBAC/RLS、服务端 API 与审计上下文。 |
| 3 | F39 | 供应链领域：组织范围的事实、确定性影响计算与受控 API。 |
| 4 | F40 | 产品 Trace 迁入：把旧 Session、Artifact、计量、回放接到组织级 SQL Trace 与 SSE 投影。 |
| 5 | F32 | CRM 业务域：客户、联系人、商机、会话、Kanban 与 CRM Proposal。 |
| 6 | F42 | 建立 Product Harness 与 `AgentEnginePort`，把现有受控协议夹具替换为锁定版本的真实 Pi Agent Core；先用确定性 Replay 模型验证工具循环、取消、失败和 Trace。 |
| 7 | F34 | Model Gateway / BYOK：将真实模型调用接入 F42 的模型端口，补组织凭证、Provider Adapter、预算、成本和审计。 |
| 8 | F35 | SQL Durable Runtime：Mission/Run/Job、Worker、lease/reaper/retry 和恢复，通过 `AgentEnginePort` 驱动 F42，不接触 Pi 内部状态。 |
| 9 | F43 | 在途事实一致性：确定到货明细与库存快照的权威来源或显式对账关系，避免重复计入或长期 unknown；待 F42 Harness 完成后再启动。 |
| 10 | F44 | 业务动作授权：将角色映射为 API capability 命令门禁；组织隔离仍由 F31 RLS 保障，scope/对象关系后续按实际业务需求引入。 |
| 11 | F36 | Specialist 与企业知识：CRM/采购 Specialist、文档权限、引用与记忆。 |
| 12 | F41 | 受控副作用迁入：旧工具、权限、审批、污点与恢复统一接入 Proposal/Execution 链。 |
| 13 | F37 | 人工决策、受控写入、发送账本与 WhatsApp/飞书渠道。 |
| 14 | F33 | Workbench 与 SSE：运行时间线、审批收件箱、配额、断线回放。 |
| 15 | F38 | Trace/Eval/交付：SQL Trace、Langfuse 可选接入、回放、隔离测试、Compose 演示。 |
| 16 | F51 | 工具声明/绑定、统一 Runtime、连续兼容组/共享容量及 Python 独占的 Pi v2 桥接已实现待验收；0030/0031 未应用业务库。 |
| 17 | F52 | 会话/工作状态、每请求上下文装配、预算、压缩与私有分块协议；长期记忆关闭也可独立验收。 |
| 18 | F48 | Mem0 候选加工/有效检索、来源版本治理、纠错遗忘、跨会话过程复用与离线巩固。 |

实施时一次只激活一个 Feature；Contract 先经用户确认，再改 `features.json` 为 `in_progress`。每项完成必须跑对应离线与真实服务验证，并更新 HANDOFF。不得因本期困难删除表中能力；只能标注 `deferred` 并说明未完成的运行证据。

## 6. 验收边界

下列情形一律不算完成：静态 UI、只创建 Run 未有 Worker 消费、没有 RLS 的“多租户”、模型可直接写业务表、审批后不重校验、只显示渠道发送但没有 send ledger、只接 Langfuse 却没有可回放 SQL Trace、或用 mock 冒充真实 Provider/渠道已接通。

真实模型、真实渠道、真实分销商/数据源调用仍受 G5 逐次授权；相应 Contract 必须把“离线验证通过”和“真实集成验证通过”分开报告。
