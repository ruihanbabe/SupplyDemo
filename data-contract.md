# SupplyAgent 数据契约与变更影响索引

版本：2。更新：2026-10-27。

本文集中记录身份、字段含义、生产者/消费者、时序、状态、权限与演进约定。业务判断正文见 [business-logic.md](business-logic.md)；实际 DDL 以 migration 为准，Feature 实施范围/证据以 Contract 为准。后续 Memory/Context/Tool Runtime 设计从本文的现有记录与 DC14 未决项开始，不从旧候选表目录开始。

## DC01 身份与归属

| 身份 | 创建者/用途 | 不可混同 |
|---|---|---|
| organization / requested_by | Python 验证用户、成员与业务对象后绑定 | 请求声明不是授权，Worker actor 不是原提问者 |
| Mission | API/SQL 创建的业务目标及授权范围 | 不等于一次模型调用或业务已完成 |
| Run / traceRunId | 产品运行与 F40 审计归集分别分配，由 runtime_run.trace_run_id 关联 | 老计量表 run_id 指 Trace Run，不是产品 Run |
| Job / generation | SQL 调度记录；generation 来自领取次数，owner 是领取实例 | 不等于角色尝试序号或持久 Context 版本 |
| attempt / invocation | F45 `intelligence_attempt.id` 同时作为本次 EngineInvocation ID；唯一 `(job_id,generation,role)` | 当前固定模板的逻辑工作为 `(job_id,role)`，不是通用 Task registry |
| toolCallId / modelCallKey | Pi 工具调用 ID；模型 key 为 invocation UUID + ordinal | 不是 Job、Observation 或新 Task；Responses call ID 的 `|` 分隔形式必须保留 |
| observation / Artifact | 实际读取与不可变产物各自 ID、版本/hash | 逻辑 artifact_key 不等于物理 artifact.id |
| workerInstanceId | 部署标签 + 每进程 UUID，存入既有 lease_owner | 不等于 Auth 用户或对话 session |

同一创建请求重投返回已有身份；同幂等键不同用户/对象/问题/分析时间拒绝。技术重试保留 Job/Run，推进 generation、创建新角色 attempt/invocation，不覆盖旧结果。独立复评/新目标需要显式新运行/授权；通用多 Run 修订与跨运行对话尚未实现。

生产者：`app.py`、`runtime.py`、`intelligence.py`、SQL 命令；消费者：Worker、Pi、Trace、结果读取、未来 Eval/Memory。关联须同组织、同对象范围；未知旧关联保留空值，不猜测回填。

## DC02 输入与入队

- 当前情报输入：organizationId、opportunityId、question（1–600 字符）、idempotencyKey（1–120）、可选 asOf、mode、modelPolicyId；准确 JSON 别名/验证由 `app.py` 的请求模型维护。
- Replay 不得携带 modelPolicyId；gateway 模式必须有组织内有效且允许实际调用的策略，并满足入口开关。当前型号/价格约束见 BL11。
- SQL 固定 Mission context_ref 的业务模式、Case/全部指定料号及可选 BOM 修订/绑定版本；入口开关后续关闭不降级已接单模式，来源绑定变化不静默扩权。
- API 只受控入队，不在 HTTP 内执行 turn。当前没有新增 session/schedule/cron 表；Web 断开不等于取消 Job。

权威：F45–F47/F49–F50、`app.py`、0024–0028；测试情报/商业/Responses 集成。策略变化影响：接单校验、幂等、context_ref、Worker 重建和前端请求。

## DC03 实际模型输入与配置

F45 角色启动前持久保存 `intelligence_attempt.input_context`：

| 字段组 | 当前内容 |
|---|---|
| 格式/执行配置 | 新 P1 快照 schemaVersion=2（旧 v1 可读）、engineVersion、modelPolicyId、modelTransport（scoped-http/local-replay）、toolAliasVersion、bindingVersion/bindingHash/toolSpecs、promptHash、contextPolicyVersion |
| 执行身份/任务 | runId、invocationId、task、instructions、analysisAt |
| 交接 | predecessors、allowedTools；业务模式下 businessScope；商业模式下 commercialScope |

它是角色启动快照；后续协议级实际请求由私有 `model_exchange.request` 保存，逐次工具读取由 Observation 保存。不能重新生成一份摘要冒充实际输入，也不能把启动快照称为完整 history/context 版本链。

工具业务名与 Provider wire name 分开，P1 别名与 Schema/版本来自 `tool_registry.py`，`model_tools.py` 仅兼容导出；后续完整绑定语义见 DC15；固定 Pi 0.73.1 负责消息/工具协议转换。角色/工具配置变化要同步 Harness、sidecar、快照、调用关联与 Eval，不单改 prompt。

权威：`intelligence.py: _role`、`harness.py`、`model_tools.py`、`gateway.py`；测试 Harness/GLM/Responses 与情报集成。

## DC04 业务事实与来源修订

| 领域 | 现有存储/字段 | 约定及变更影响 |
|---|---|---|
| CRM | crm_customer/contact/opportunity/conversation/task；version；owner_id、assignee_id、due_at | 同组织成员关联；缺负责人/截止显式未知；事实 Repository 只返回当前 scope 最小投影与来源引用。影响 BL02/BL04、CRM/情报工具 |
| 供应 | supply_case、inventory_position、supply_demand、supply_receipt | 数量/date/null 保留真实含义；来源明细和聚合在途不可默认相加。影响 BL05、F39/F43 |
| 产品/BOM | catalog_product；bom_revision 的 revision、lines、content_hash、source_kind、revoked_at | lines schema v1：lineKey/manufacturer/mpn/quantityPerUnit；发布不可变，数据库生成 hash；synthetic/official/user_supplied 来源不可混用。影响 BL06 |
| 商机绑定 | opportunity_bom_binding：opportunity、bom_revision、units、required_on、version | 绑定变更推进版本，禁止删除重建重置；范围/版本接单时固定。影响投影和旧结果判断 |
| 报价 | commercial_quote_revision：reference/revision、quote_kind、BOM/可选行、quantity、unit_price/currency、issued_at/valid_until、estimated_ship_on/confirmed_arrival_on、stock_reserved、source_kind/scope/revoked_at | 内容不可改写，纠正新版本；scope 可变且读时重验；报价不等于收货。影响 BL07 |
| 知识 | knowledge_document/ACL、document_version、chunk、version_revocation、document_withdrawal | 文档版本不可变，撤回/权限当前生效；来源引用定位版本+位置+片段 hash。影响 BL08 |
| 旧记忆 | long_term_memory：content/source_ref/source_kind/source_version_id/status/proposed_by/confirmed_by | 当前 F36 兼容；不等于 Mem0 已集成。未来治理来源/同步映射另见 DC14 |

DDL 对应 0001–0022、0025–0026；代码 `crm.py`、`supply.py`、`business_nodes.py`、`commercial.py`、`knowledge.py`、`memory.py`。源变更需要复核实际观测、旧答案可读性、记忆和下游结论，不能只刷新 UI。

## DC05 工具交付与 Observation

生产者：按提问者当前权限执行的领域只读服务/确定性计算。持久落点：`intelligence_observation` 的 id、attempt_id、organization_id、mission_id、tool_name、arguments、payload、content_hash、observed_at。

- 先受控保存实际结果再交给模型；hash 由 PostgreSQL 计算，写入验证活动 Job owner/generation/lease。unknown/partial/error 不改成空数组或成功事实。
- payload 保留源引用/计算范围/分析时间及实际政策；`payload.execution` 为 invocationId、toolCallId、modelCallKey，Replay 可没有 modelCallKey。
- F36 的工具结果另用 F40 Artifact；其 Citation 指向实际 Artifact ID/版本/hash，不伪造为 F45 Observation。

消费者：模型、后序诊断、引用校验、工作台、未来 Eval。修改返回字段/计算口径需联动 `intelligence.py`、领域服务、结果 excerpt、引用校验、Pi 工具结果与相关集成测试。

## DC06 Claim、引用、Review 与结果

- Citation 固定 sourceKind、sourceId、version、locator、fragmentHash。F45 observation 引用为 `version=1/locator=payload`；知识/BOM/报价引用由对应验证器重验当前来源。
- F45 输出包括 role、invocationId、complete、claims、unknowns、conflicts；Claim 含 subject、claimType、conclusion、quality、citations 等。精确大小与枚举由 `specialists.py` / `specialist_runner.py` / 角色指令约束。
- 有效引用只证明记录与来源相符，不证明结论语义充分。F45 最终质量为 needs_review；F36 独立 review/父 Recommendation/ProposalDraft 另有固定门禁，不等于可执行批准。
- 只接受活动代次产物。硬退出可能留下 running attempt/exchange，不因记录存在就向后续 Context/Memory 接受为完成结果。

权威：`specialists.py`、`specialist_runner.py`、`intelligence.py`、`mission_runtime.py`、`trace.py`；影响回答投影、旧结果读取、记忆候选来源与质量评测。

## DC07 Model Gateway、私有交换与计量

- ModelCapability 当前绑定 actor（Worker）、organization、Trace Run、policy、expires。它不绑定 Job/generation/invocation，不保证取消或失租后立即撤销；call key 格式/唯一性也不等于活动 attempt 已被逐请求授权。
- `/v1/model` 返回实际模型/API/兼容描述与 policyHash；请求重验策略并在预算预留时锁定。GLM 走 Chat Completions，OpenAI 走 Responses。
- model_exchange 关联 reservation_id、trace_run_id、actor_id、invocation_id、call_ordinal；保存 schema_version、有效 request、policy_snapshot、response、state、created_at/finished_at。私有正文不写组织公开 Trace，也不无条件开放给原提问者或未来 Mem0。
- model_reservation/model_usage 是成本权威；settle 与最终归档原子提交。缺 usage 保留预留，硬退出可留 running；不自动对账或释放可能已计费调用。
- Provider Key、Gateway 能力、SQL/渠道凭证不进入模型上下文、报告或前端。

权威：`gateway.py`、`glm.py`、0018、0027–0028、F49/F50；影响预算、关联、私有 RLS、模型消息/Trace/Eval。

## DC08 分层状态与接受时点

| 对象 | 当前状态/含义 |
|---|---|
| Job | queued、leased、completed、failed、cancelled；只表达调度/执行，不等于业务目标达成 |
| F45 attempt | running、completed、failed；角色与 Job/generation 关联 |
| model_exchange | running、completed、incomplete、failed、interrupted；工具调用结束可结束本次模型请求，不代表业务 Run 完成 |
| Claim/Recommendation | supported/conflicting/stale/unsupported/needs_review/rejected 等质量；准确枚举见 specialists.py |
| 旧记忆 | candidate、confirmed、revoked、pending_revalidation；确认/来源有效性与内容存在分开 |
| 报价 | 状态由 BL07 确定性派生，不以模型输出覆盖来源 |

失败后的 queued/failed/cancelled 在转换事务持有行锁时读取，供报告使用；别的 Worker 再次领取不能改写本次报告含义。模型/工具调用不持有跨网络长 SQL 事务；有效交接先持久化并通过租约/版本门禁再供下游消费。

## DC09 时间、版本与因果

as_of/analysisAt 是业务分析时点；observed_at 是实际记录观测时间；created_at/finished_at 是执行记录时间；reportedAt 是进程报告生成时间。历史分析时点不意味着能查询当时完整数据库状态。

schemaVersion 表达格式；generation 表达领取代次；来源 revision/version 表达事实修订；promptHash/contextPolicyVersion 表达配置。因果用实际引用/前序关系定位，不仅按时间戳排序。旧历史缺失不猜补，未知不填零。

影响：所有输入、Observation、BOM/报价/知识、报告和评测；字段变化需要兼容读取，实际新持久语义使用新格式版本或追加 migration。

## DC10 数值与 wire 表示

数量/价格按确定性 Decimal 或数据库 numeric 保存；当前 BOM 乘加使用 48 位 Decimal 精度。wire 通过受控 JSON 投影将 Decimal、UUID、日期/时间转换为字符串，不让 LLM 重算/补写数值。

时区必须明确；供应事件使用业务时区的 date，报价/任务使用带时区时间。货币、数量单位、确认/预计属性不能丢失；未知 null 与 0 不同。变更影响算术、SQL 约束、API、工具结果、前端展示与边界测试。

## DC11 权限、来源有效性与生命周期

- 每次访问组织事务校验当前身份、成员与对象范围；知识/报价/BOM 可见性和撤销再次校验。历史快照不能绕过当前源权限。
- 原始输入、模型交换和私有观测按各自 RLS 管理；公开 F40 Trace 仅脱敏元数据/引用。SQL 是权威，Redis、Node session、页面内存或日志不是恢复事实。
- 旧 memory candidate 不注入有效记忆；来源改变转待重验，撤销阻止使用。F48本地合成同步/遗忘/保留实现及生产限制见DC19/DC20与Contract。
- 业务数据及私有输入的统一保留期限、跨来源撤权传播和完整删除策略尚未冻结，不默认永久保存或删除级联。

影响：`tenant.py`、领域 RLS、`knowledge.py`、`memory.py`、`intelligence.py: read_answer`、未来 Context/Mem0 Adapter。

## DC12 存储归属与演进

- 产品 `app` schema 只由 `supabase/migrations` 管理，legacy public 表只由 Alembic 管理。旧 migration 历史保留，不重排、不将两套工具交叉管理同一表。
- 0023 是未应用到业务库的审批草稿；0024–0028 是已通过克隆验证但业务库仍未应用的新链路。不能把文件存在当成库中已有对象。
- 本次清理追加 legacy 0009 和产品 0029，仅退役已批准的空旧表/精确旧函数；不删除 hw_part、记忆/CRM 提案、pgcrypto、public schema 或集群角色，不自动应用 0023–0028。实际应用证据见 F30。
- `app` 的精确字段/约束以 DDL 为准；本文定义数据含义和跨模块关联，不复制一套可与 SQL 漂移的完整建表脚本。

## DC13 运维报告与探针

Worker v1 报告字段：schemaVersion=1、reportedAt（UTC）、organizationId、workerInstanceId、outcome、jobId、generation、runId、traceRunId、errorCode。没有取得身份时相应字段 null；禁止正文、密钥、能力或原始异常。

outcome / CLI 退出码：idle/completed/cancelled → 0；claim_rejected/retry_scheduled/failed/lease_lost → 1；process_error → 2。idle 只说明此次未领取可执行任务；retry_scheduled 是 SQL queued 的报告投影，不是新 DB 状态。Make 的失败 recipe 通常自身退出 2，调度需结合 JSON outcome。

Gateway 私有 `/health` 仅存活；`/ready` 只读检查受限数据库/结构/权限，返回 ready 或 unavailable，不调用 Provider、不产生费用。启动/命令/运维限制见 [运行说明](docs/design/model-gateway-worker-contract.md)。报告不入业务表，也不默认进入模型或 Memory。

## DC14 当前可复用机制与后续边界

| 方向 | 当前可复用 | 仍需设计，不预建表 |
|---|---|---|
| Context | F52 C1–C3本地实现、完整消息/Working State/每请求快照/压缩与渐进读 | 默认关闭，生产启用及自动业务续接另行验收；迁移清单见F52 |
| Memory | 旧候选/确认/撤销/待重验、知识不可变版本/ACL、BL09/F48 | F48/DC19/DC20；0035–0041本地合成物理实现，生产部署/分类保留配置未交付 |
| Task/计划/恢复 | Mission/Run/Job、固定角色、R1失败分流/固定checkpoint hooks/Job绑定模型能力 | 通用Task/计划修订、人工等待/局部复用、已发出请求撤销和生产恢复治理仍后续 |
| Tool Runtime | 精确工具 allowlist、参数验证、别名版本与模型/工具关联 | F51/DC15–DC17 已实现 Registry/Runtime/Scheduler/v2 批次与克隆证据；MCP/Skill 动态注册与 R1/R2 授权另行设计 |
| 依赖/协作/Eval | 固定来源/Claim，F38 E0/E1案例runner/私有快照/硬约束与人工评分记录 | 动态依赖/冲突裁决、Judge、真实模型质量消融、全回放分叉仍后续 |
| 业务动作 | F41人工提案/审批/SQL执行、F37加密fixture发送/回执/对账，DC22 | 自动Agent R1、真实采购/WhatsApp/飞书及会话/审批UI、生产授权和对账另行接入 |

本节保留旧 schema 目录中的有效问题，不保留尚未批准的候选表名作为硬性实现清单。新增数据先列生产者/消费者、身份、版本/时间、状态转换、接受时点、权限/生命周期和验证案例，再确认物理清单与 migration。

## DC15 工具定义、绑定与执行身份（F51 目标）

F51 P1–P4 已实现待验收；0030/0031 仅在克隆库验证，业务库未应用。bindingHash 指工具声明、Schema/版本及静态调度声明的指纹，不包含授权/lease 或业务 scope，不是授权令牌。兼容 predicate 行为变化须提升 specVersion；实际调度配置另以 policy schemaVersion/strategy/configurationHash 留存。

| 对象 | 生产者 → 消费者 | 目标字段与约定 |
|---|---|---|
| ToolSpec | 受信任 Python 注册配置 → Registry、Runtime | canonicalName、specVersion、description、inputSchema、显式 aliases/wireName、riskLevel、sideEffect（read_only/internal_write/external_action）、overlapPolicy（默认 exclusive）、resourceRequirements。名称/别名全局不冲突，规范名不能占其他工具别名；Schema 与 validator 必须以相同用例验证，业务验证保留在 Adapter |
| ToolBinding | Registry.bind → Harness/Runtime、Pi/Gateway 受限投影 | bindingVersion、bindingHash、角色、有效工具与精确 specVersion、aliasVersion、已验证 scope/绑定修订；不可变。模型只收到 name/wireName/description/inputSchema，不能看到凭证、未绑定目录、内部资源键或授权实现 |
| CallContext | Worker/业务服务 → Runtime/ResultStore | organizationId、requestedBy、workerActorId、workerInstanceId、missionId、runId、traceRunId、jobId、generation、invocationId、业务 scope/修订、analysisAt、取消/截止与执行接口。Python 绑定，不能信任模型同名字段 |
| PreparedCall | Runtime.prepare → Scheduler/Runtime.execute | 调用身份、深拷贝有效参数/hash、规范名/specVersion、Binding hash、按有效参数计算的兼容性和资源需求。进程内私有值，消费一次；不能由 JSONL 提交 |
| batch / call | sidecar 完整 assistant 消息 → Python | batchId 是 invocation 内单调序号；index 是 toolCall 原位置；callId 是不透明原始 ID；modelCallKey 指实际模型请求，Replay 可空。不能用时间戳或抵达顺序代替批次 |

现有七工具声明以 `tool_registry.py` 为单一来源，`model_tools.py` 为生成兼容导出；F51 不增加业务工具。F36 的 supply.impact/knowledge.search 与 F45 绑定工具不能因作用相近而合并；版本变化要进入实际 input_context 和模型投影。新增快照格式使用 schemaVersion=2 保存 bindingVersion/hash、有效 specVersion、aliasVersion、实际调度策略版本；旧 schemaVersion=1 继续可读，不伪造回填。

副作用、兼容性、资源需求三者独立；没有显式配置时 exclusive。当前 resourceRequirements 为服务端静态 ToolResource 声明，兼容 predicate 可使用校验后的参数；未来参数化资源仍须由受控投影得到，不能包含任意 SQL/文件路径或模型自选资源组。只读不推导并发，MCP 提示不进入执行授权。当前仅 read_only/r0 可绑定；其余声明保留表达能力，执行仍拒绝。

## DC16 调用生命周期与持久结果（F51 目标）

0030 已实现 **app.tool_execution**（P2，未迁移业务库）：生产者 Runtime/受限恢复流程；消费者 Runtime、Worker 审计、后续 Eval。它是私有调用台账，不取代 Job、业务 Observation/Artifact 或模型费用账本。

- 身份：id、organization_id、requested_by、worker_actor_id、mission_id、run_id、trace_run_id、job_id、generation、invocation_id、batch_id、call_id、call_index（协议 index）、model_call_key（可空）。唯一 `(invocation_id,call_id)` 及 `(invocation_id,batch_id,call_index)`；所有 FK/关联验证同组织、同 Job/代次，不仅检查 UUID 存在。v1 按单次调用生成单项批次，不猜测模型批次。
- 内容：schema_version、canonical_name（未知可空）、requested_name、spec_version、binding_hash、arguments_hash、scheduling_snapshot、state、error_code、result_kind/id/version/hash、created_at/started_at/finished_at。有效参数/业务正文继续保留在各自私有 Observation/Artifact；台账不复制正文，不另存一份不受控工具历史。无法安全规范化的非法参数只记拒绝与受限元数据。
- 状态：准备通过 → prepared → running → succeeded/failed/cancelled；准备拒绝直接 rejected；硬终止前尚未执行的已准备项可 cancelled；崩溃留下 prepared/running，由后续受限回收流程核对 owner/generation 后标 abandoned。终态不可覆盖，abandoned 不能重新执行为成功。台账缺少终态或引用即不可作为成功依据。
- 接受：Runtime 调用 Adapter，ResultStore 在活动 owner/generation/lease 及当前权限/绑定通过后保存实际业务结果，并与成功台账终态在同一接受事务关联。F45 复用 intelligence_observe 和 DB hash；F36 使用原 Artifact 及真实 ID/版本/hash。若现有写接口不能共享接受事务，P2 必须调整内部持久接口，不能先发结果再补关联。错误记录无事实 resultRef；合法空结果可有真实证据引用并标 unknown。
- 失租：旧 Worker 的写入/结果接受一律拒绝，不能为了记录失败绕过活动代次门禁。其记录由受限恢复身份审计为 abandoned；新 generation 建新 invocation，不复用旧结果。技术恢复不承诺任意工具 exactly-once，当前限定 R0。
- ToolOutcome：status=ok/error、稳定 code（成功可空）、modelPayload（有界业务结果或安全反馈）、resultRef（可空）；属于系统硬失败的异常不包装成普通 error 继续。外部 result 字段使用该 envelope，错误策略见 BL12。
- 安全与生命周期：原用户仅在当前对象授权允许时通过受控读取访问自己的调用元数据；Worker 只可按组织与活动租约写；其他成员不能借组织级 RLS 读取他人私有台账。直接表写入拒绝，读取由 RLS 限定；权限命令/投影检查原用户及 scope。公开 Trace 只保存 ID/code/状态/引用，不能保存参数、正文或能力。记录保留与删除沿 DC11，业务删除不能静默级联删审计；本期不另设自动保留期限。

精确 DDL/受控命令/RLS/索引在 0030_f51_tool_execution.sql；无历史回填，已先克隆验证，不自动迁移业务库。既有结果保持可读，新增台账仅覆盖启用后调用，不追溯伪造旧记录。调用预算按原始请求计数，包括 prepare 拒绝；同 ID 重投是协议失败，不是幂等重执行。

## DC17 共享资源容量与取消（F51 目标）

0031 已实现 **app.tool_resource_slot**（P3，未迁移业务库），采用现有 PostgreSQL 原子准入，不新增 Redis 锁或独立调度服务。生产者为服务端部署配置/受限准入命令；消费者 Scheduler；模型不读写该表。

- 资源身份是服务端配置的 `(scope,name)`：organization 或 deployment；实际连接/session 必须映射同一资源。实现采用加权占用记录而不预建 N 条空槽：id 主键，唯一 `(execution_id,resource_key)`；resource_key 编码组织/部署作用域，保存 organization_id、worker_actor_id、owner_token、units、capacity、expires_at、state=held/quarantined/released 及时间。Job/generation 通过 execution_id 取权威值，不复制可漂移身份。固定顺序池锁下约束活动/隔离 units 总和不超 capacity；空闲是没有活动占用。相比预分配槽，更直接支持多资源/多单位原子准入。跨组织占用详情只在受控函数内部可见。
- 同一调用所需全部资源按固定字典序、在短事务内原子取得，拿不全则全部不占；不得持锁等待外部请求，避免跨资源死锁。等待受取消/截止和 Job 活动性约束；公平性仅保证最终在截止内成功或明确失败，不承诺严格 FIFO。
- running 前再次核验 Job 与 owner token；释放必须匹配原 owner token，迟到旧 Worker 不能释放新 owner 占用。资源租期不能超过 Job lease，活动 Worker 续租同时验证 generation、owner token、未过期及 held 状态；续租失败取消操作并隔离占用。
- **占用超时不等于远端停止。** Job 失租/进程崩溃后，held 转 quarantined，不自动变 free。只有确认本地操作已终止、连接/session 已销毁，或受控运维确认并使旧执行失效后才释放；无法确认则继续拒绝准入。不能仅靠 TTL 回收宣称跨进程并发上限成立。正常协作取消也要完成清理后再释放。
- 没有资源声明不额外建立槽位。P3 的直接批次测试只为已验证可隔离连接的工具开启 overlap，未知连接默认互斥。并发容量不是每秒请求配额；未来供应商接入须单独定义已有配额/限流 Adapter，未定义时不开放该资源并行，不用 semaphore 冒充 QPS 限流。
- PostgreSQL 无法准入或写调用台账时失败关闭，不回落成仅进程内锁。进程内 semaphore 可用于测试和本进程减少竞争，不能作为部署共享容量证据。

P3 migration 只追加资源槽位和受限命令；不赋予业务写入权。新增资源/容量变化需受信任配置，不提供模型/前端修改入口。当前暂停的 R1/R2 不会因取得槽位获得执行权限。验证涵盖独立进程竞争、跨组织共享资源、取消等待、多资源原子取得、租约失效隔离、旧 token 拒绝与明确释放后再准入；不模拟供应商已接入。

## DC18 会话、消息、工作状态与提交水位（F52 目标）

DC18 的 C1 三项存储责任已由 0032 实现并通过克隆验证，未应用业务库；DC21 的 C2 每请求快照已由 0033 实现并通过克隆验证；DC19/DC20 及 C3 压缩/读取仍为开发目标。不改写 DC01–DC13 对当前实现的描述。所有关联均验证同组织、同原用户/允许参与主体及当前业务范围，不只检查 UUID 存在。

| 计划物理责任 / 单元 | 生产者 → 消费者 | 身份、内容、状态与接受时点 |
|---|---|---|
| app.context_conversation / C1 | 受控业务命令 → ContextStore/后续 memory | id、organization_id、owner/参与主体与对象 refs、state=open/closed、committed_watermark、版本/时间；显式续接才共享历史，成员身份不等于对象权限 |
| app.context_message / C1 | Python 接受的用户/Pi/工具消息 → buffer/来源加工/审计 | id、conversation_id、ordinal、message_unit_id、kind/role、invocation/model_call_key、batch/call/index、payload 或原产物 ref/hash、source label、created_at、保护/保留分类；唯一 conversation+ordinal，幂等键同内容返回原提交 |
| app.context_working_state / C1 | Worker/验证过的模型增量 → 恢复/Compiler | run/job/generation/version、state schema、source_watermark、accepted_evidence refs、previous_version、kind=working/checkpoint/compaction、coverage/hash；版本追加，checkpoint 作为同责任内记录，不另立第二个任务权威 |
| app.context_snapshot / C2 | Compiler → Pi/Gateway/Eval | request/snapshot 身份、私有消息投影与逐块 manifest，具体字段见 DC21；不是角色启动快照 |

一条 assistant 多调用消息及其全部终态 toolResult 形成完整单元；原始事件可逐条接受，只有已闭合单元进入可压缩范围。大结果复用 Artifact/Observation 正文，message 只存受控投影/ref，不能复制为公开 Trace。活动工具身份继承 F51/DC15/DC16。拒绝调用可保留原始 JSON 非对象参数（例如 null），但必须匹配 rejected 台账及参数 hash；成功调用仍要求对象参数。

提交轮次时锁定 conversation，expected_watermark 必须匹配；新增消息/已接受产物、commit watermark 和可加工 source/outbox 在同一事务接受。F48 关闭/未部署时先以 conversation durable watermark 保留待捕获区间，F48 启用后补建唯一工作项，不制造 C1 对未来表的运行依赖。显式记住命令尚无后端时不可报可检索成功。

工作状态含目标/范围修订、约束/分析时点、步骤状态、证据引用、已验证发现、未知/冲突、阻塞/人工决定、下一步。活动 Job owner/generation/lease 和 expected_version 共同门禁；旧 generation 不能覆盖。compaction 覆盖游标、已提交水位、memory 提取消费游标分开。恢复到新 invocation 重验来源和对象；中断消息保留事实但不伪造合法成功配对。

RLS/受控命令按 owner/允许主体与对象权限；Worker 仅按活动租约接受消息/状态，读历史也不扩大原用户可见性。正文/快照均私有，删除依 DC20；保留不可变身份不表示正文永久不可擦除，可保留不含内容 tombstone。

## DC19 记忆来源、不可变版本与关系（F48 目标）

| 计划物理责任 / 单元 | 生产者 → 消费者 | 字段与不变量 |
|---|---|---|
| app.memory_source / M1 | source commit/capture → Worker/引用/删除 | id、organization/原用户、source kind/id/version/locator/hash、source watermark/event_at/observed_at、实体/安全域、source label、data_class、revocation/deletion 状态；原文不无条件复制，实际正文读原授权来源 |
| app.memory_version / M1 | 提取候选/确认命令 → recall/发布/审计 | 稳定 assertion_id + 递增 version、organization、content_kind=process_summary/stable_memory、trust=generated_reference/unconfirmed/confirmed、可选 subject/predicate/normalized value（摘要不要求）、准确正文/hash、scope、valid_from/to、status、确认方法/actor、policy/extractor version、created_at；唯一 assertion+version，同内容变化追加，hash 由 DB 计算 |
| app.memory_evidence / M1 | 验证器 → 引用/失效传播 | memory version ↔ source 的 support/contradict、片段/hash/时点和权限条件；M1 即可多来源，不能靠 Mem0 文本 hash 代替关系 |
| app.memory_relation / M2 | 受控治理/Dream 候选接受 → 当前投影/删除 | duplicate_of/supersedes/contradicts/derived_from/merged_from、起止版本、原因/actor；同组织且不扩大来源权限；M1 的 supersedes 可先作为 memory_version 前序指针，M2 接入关系图时保留同一语义 |

SQL 是准确摘要/确认内容、当前状态及关系权威，Mem0 是可重建候选与有效检索表示，不另做 SQL memory 搜索引擎。entity ID 由业务绑定，不按文本同名合并；时间包括 source event、observed、记忆 created、valid interval，不能混用“最新”。信心/证据级别是描述，不签发数值/权限。

过程摘要通过来源/权限/PII/质量门槛后进入 ready（trust=generated_reference，无 confirmed_by），稳定命题 candidate→active/rejected（active 需确认）。ready/active 均可转 superseded/revoked/expired/pending_revalidation/quarantined；历史参考 ready 不等于事实确认 active。内容版本不可覆写，非正文状态按受控命令演进；确认、关联和替代当前投影原子接受。published/indexed 是外部投影状态，不能并入 active 布尔值。source 失效时立即 deny 并推进 epoch，后端更新完成不是撤权前提。

每条引用可定位 memory assertion/version/hash，并通过 evidence 回溯原始 source；F36 Artifact/F45 Observation 引用保持原身份，memory 不是替代其引用验证的新证据权威。按 ID 取回也重验当前 source/ACL/有效期；不可见来源不泄漏标题/摘要。

M1 合成实现（0036）：Conversation 新提交区间按完整 message_unit_id 分块，memory_source 的 start_ordinal/watermark 为包含首尾的区间，source_hash 只覆盖该区间。每个范围及工作项与来源 commit 同事务；重复 commit 不生成新范围，关闭会话推进所有尾批到期时间。分块不得截断 assistant/tool_result 配对；单个超限或未闭合单元隔离，不能截断后宣称覆盖。

memory_version.summary_manifest 保存策略版本、sourceId/hash、起止水位及每个闭合单元的 ID/范围/hash，不复制正文。范围由数据库生成，L1 返回 coverage，召回来源返回 startOrdinal/watermark。同一会话多个不重叠 process_summary 可以同时 ready；版本号表示追加顺序，不使其他独立区间失效。只有完整覆盖旧区间的新版本才替代该旧投影；旧前缀不能覆盖已索引的新完整区间。旧数据的空 manifest 不当作已有覆盖证据；本单元不批量回填或切换 F36。

M1 新增 memory_source/version/evidence；M2 新增 relation，旧 source_ref/source_version_id/confirmed 状态通过显式迁移映射与对账，不猜测确认或内容。M1的精确索引/RLS/受控命令已在0035–0038和F48验真；其余后续单元仍先提交物理清单，不从目标文档推断已建表。

## DC20 加工、后端投影、删除与成本（F48 目标）

| 计划物理责任 / 单元 | 生产者 → 消费者 | 内容/状态 |
|---|---|---|
| app.memory_work_item / M1 | capture/确认/撤销/任务终态 → Worker | id、organization/security domain、operation（summarize 为默认，extract_stable 为按需）、source range/hash/watermark、memory version、policy、dedupe key、originating_job/generation、独立 runtime_job ref、status、attempt/next_due/error；状态 pending/leased/review_pending/publish_pending/indexed/retry_wait/reconciliation_required/failed/quarantined/cancelled |
| app.memory_projection / M1 | Adapter/对账 → recall/删除 | memory version、backend/制品版本、collection/namespace、external_id、publication_key、content_hash、backend_epoch、state=pending/indexed/unknown/retired/deleting/deleted；同版本可登记重复副本再回收，不能遗漏清理目标 |
| app.memory_delete_request / M1 | 有权忘记/失效/保留命令 → deny/清理/审计 | request id、组织/actor、mode/reason、目标 refs/依赖闭包、水位、每目标存储清单/状态/验证时间、backup_pending、非正文 tombstone；state=requested/blocked/purging/backup_pending/completed/failed |

捕获 source range + policy/operation 的唯一 dedupe key；发布按组织+memory version+backend_epoch 的 publication_key 串行化。独立 Memory Job lease 决定本次加工接受，原业务 Job 仅作因果引用；索引延迟不保持业务 Run running。旧 Worker、来源失效、删除请求在外发前/接受前重验；Mem0 写入未知先对账，不因 outbox 去重声称 exactly-once。

candidate/active 分别登记独立 vector collection/history-message store 与 namespace；可信身份映射覆盖组织/用户/精确对象域。敏感原文不落公共 Trace。查询外部结果 hydrate SQL 版本，只有（process_summary ready 或 stable_memory active）+indexed+当前权限/来源/有效期通过可供 Context，并保留 trust 标签，缓存仅存候选并带 epoch。

hard delete 清单至少涵盖 SQL 内容、候选/有效向量、Mem0 history/messages/entity、原文授权范围、context payload/checkpoint/summary/cache、导出/备份范围。请求先原子 deny、推进 epoch、阻止旧输入再加工，后逐目标验证。多来源移除支持边并重建派生文本；仅有独立合法支持的剩余内容可保留，不能保留已删来源独特信息。completed 仅在声明范围都验证且备份要求满足后记录；不可删除的业务/审计保留依据显式显示，不写入正文 tombstone。

分类保留 policy 必须给 source、candidate、active/episode、history、snapshot、备份各自期限/处理方式；未提供生产值则真实数据准入关闭，synthetic fixture 用明确有限策略。恢复备份先重放删除 ledger/deny 才能对外查询；仍有备份副本则 backup_pending，不把普通 delete 返回当完成证据。

提取/摘要/embedding/rerank/Dream 使用 operation_id/job/policy/model/usage/cost/状态关联的私有计量记录；模型调用复用现有 reservation/usage，embedding/reranker 需在实施清单明确补充受控端口和计量，不假定当前 Gateway 已有路由。外部调用先授权/预算预留，缺 usage 不当零费用，不因 Mem0 自动触发绕过 G5。凭据不进入 work payload、memory 或 Node。

M1 0037 实现补充：app.memory_operation 保存 actor/namespace、work/generation/source_epoch 或 deletion_id、policy/model/input_hash、预留上限、input/output usage、cost与状态。独立事务预留/结算；未知结果不回收为零费用。合成政策的已知价格为零，实际端点/价格/真实数据启用仍受部署前置约束。

M1 0038 实现补充：app.memory_dependency 保存已消费版本→会话的组织内精确域依赖；app.memory_recovery_state 保存blocked/ledger_applied，app.memory_replay_log按组织+原删除request去重。三表私有，模型没有写入口。snapshot准备时同事务登记；来源读取和加工追溯上游，forget的assertions/conversations闭包保存在原delete_request.inventory。关闭相关会话并取消旧outbox以阻止迟到写入，hard清除依赖正文/快照/checkpoint和交换；未知备份仍backup_pending，不能宣称全局物理删除完成。

七天合成保留由Worker逐步处理版本、候选、原来源与投影/history，非正文治理/删除/计量记录作为恢复依据保留。恢复必须在流量关闭时保留最新独立删除日志，校验并重放后处理当前存储清理，ledger_applied及requested检查通过才解除屏障；真实分类期限/备份清单仍须上线配置，不从合成数值外推。

## DC21 Context 快照、预算与消费（F52 目标）

C2 已实现 0033（仅克隆验证）：context_snapshot 实际以 id 为主键，invocation_id+model_call_key 唯一，关联 conversation/job/generation/worker owner。role/scope 由 conversation 关联读取，模型窗口/估算器/预算与 manifest 在私有 projection 内保存，模型政策另存 hash；source/ACL 当前共用组织级 source_epoch，MemoryPort 关闭，独立 Memory epoch 和摘要依赖仍属后续。model_exchange 实际追加列名 context_snapshot_id/projection_hash/request_hash。

完整目标 app.context_snapshot 按 snapshot_id 唯一、invocation+model_call_key 唯一关联：organization、requested_by、run/job/generation、role/scope、schema_version、context_policy_version、model policy/profile/tokenizer、memory/source/ACL epoch、source watermark、消息/区块 manifest、取舍原因、预算、projection_hash、state、created_at/accepted_at。manifest 保存已选版本/ref/hash和必要私有投影、排除 reason、来源依赖，不只保存摘要计数。

状态 prepared→accepted/invalidated/cancelled；Gateway 接受本次发送时检查同 actor/组织/Job/invocation、活动性、profile/policy/epoch，绑定一次 modelCallKey。model_exchange 追加 snapshot_id/projection_hash/request_hash 关联，Replay 可无 exchange，但保留 snapshot 与未外发状态。projection_hash 表达 Compiler 消息投影，request_hash 表达 SDK 最终 Provider body，不能混称相等；两种协议的允许映射需回归验证。

预算按实际模型/部署 window 扣 output/reasoning、Schema/协议开销、安全余量和输入 cap；记录每区 token/bytes、估算方法/版本、强制保留项、阈值及压缩覆盖。当前输入启动 schemaVersion=1 仍可读，新增 snapshot schemaVersion=1 是不同对象，不能伪造旧角色快照为新请求快照。

source/ACL/memory epoch 变化立即使未消费 prepared 失效，已持久摘要和已选缓存仍按依赖过滤；发送前最后检查不意味着能撤回已外发内容。snapshot 正文可依删除策略擦除，保留 ID/状态/删除证据，不因“审计”永久留敏感消息。

F52 C2 新增 context_snapshot 并追加 model_exchange 关联；候选表名不是已存在对象，按实际下一产品 migration 编号实现，不预占 0030，不改 legacy 历史。C1/C2、F48 M1/M2 每单元先列完整列/约束/RLS/受控命令/索引及回填/回退清单，再克隆验证和独立提交验收；业务库应用仍另行确认。

### F48 M2物理增量（仅克隆验证）

0039为memory_version增加event_at/valid_from/valid_to/conditions，发布前受控设定，发布后需新版本；memory_relation保存版本对、kind、actor与时点，私有RLS，原文正文不放关系边。派生/重复追加原支持，冲突不签发最新值，删除闭包覆盖共享来源。0040 source.legacy_id唯一映射旧记录，memory_read_route按组织/原用户选择唯一读取路径；当前来源知识版本/ACL/撤回持续核验，迁移前导出旧状态与来源，confirmed须显式复核+原投影对账。回滚遵守删除tombstone；include_source硬删清旧正文。真实数据迁移和部署切换仍未执行。

### F48 M3物理增量（仅克隆验证）

0041 memory_derivation保存source→输入version，memory_dream_batch保存安全域、原始支持指纹、固定策略与无正文消融计数；Dream任务复用memory_work_item，不另造调度器。version.temperature区分hot/cold，冷层只降排序不失去删除/来源门禁。Dream输出只为generated_reference，稳定经验另建未确认候选。source_ancestors同时追溯Context消费及派生图，roots排除转述/派生本身；不把来源数量当统计置信度。

forget inventory.sources包含尚无版本的派生副本；soft先deny/cancel，hard清正文，恢复以最新独立ledger重放同一闭包。已恢复多个requested必须全部清完才解除blocked。batch/关系/计量/删除记录不保留被删正文。固定lossless-dream-v1≤30输入/8000字节，无损合并保留全部时间/条件；仅有精确去重或槽位覆盖收益时接受合成批次，默认Dream关闭。

## DC22 人工提案、执行与fixture发送（F41/F37当前切片）

仅克隆追加0042–0044，业务库未迁移。0023草稿历史保留，装配一次；0043/0044受控函数/表在克隆重复应用验证。规则归BL10，接口/运行说明归ACTIONS_RUNTIME.md，不以本文表名代表真实渠道已接通。

| 对象 | 生产者/消费者与身份 | 状态及接受边界 |
|---|---|---|
| action_proposal / action_approval | 人工API创建/独立人工决定；组织+对象scope+请求者；内容/目标版本/证据/到期/revision由SQL hash绑定 | pending_review/approved/rejected；短期authorization_id仅数据库收据，不给模型；approved不代表执行。消息content只含密封payload、keyId、HMAC语义tag与fixture标记 |
| action_service_policy | 受控部署配置，默认缺省禁执行；revision/ enabled/max_approval_seconds | 变更推进revision，不删除重建旧版；无政策草稿revision=0。当前缺人工配置UI，不能由模型或渠道回调签发 |
| action_execution | 审批同事务产生唯一proposal_id/logical_action_id/Job与独立Trace；消费者F35 Worker | pending/succeeded/rejected；0042有效owner/generation/lease行锁。customer_update同事务效果+账本；重投读原结果。执行Job与原证据Trace分别定位，非共用一条runtime_run |
| channel_send_ledger | F37 Worker先写意图再Adapter；unique proposal/logical_action_id | sending/accepted/delivered/failed/reconciliation_required/rejected。未知查对账不盲发；accepted不等于送达；delivered/failed/rejected终态不由迟到通知随意逆转 |
| channel_inbox | 经原始报文HMAC/时间窗与严格字段验证的fixture Receiver；组织由服务端Worker身份固定 | (organization_id,event_id)幂等，变更同ID内容冲突；密封payload+语义tag，仅Worker读取。记录实际外部结果不自动把cancelled Job转completed |

消息读取先重验当前客户scope，限定请求者或可审批者；解密后检查组织/请求者/目标/版本与密文tag绑定。输入地址是fixture数据，不解析URL或选择真实账号。Provider/协议不由模型或用户任意字符串切换。密钥只在可信Python配置，不进入SQL/Node/Trace；当前HMAC是fixture协议，非实际WhatsApp/飞书验签。

后台未知结果、Job失败/取消、实际送达是不同事实：例如Job取消后迟到回执可以更新发送账本，但原Job仍cancelled；重试耗尽仍可保留reconciliation_required。真实渠道的会话关联、密钥轮换/备份与保留、账号授权和协议校验另行验收；不能从fixture推断跨系统exactly-once。
