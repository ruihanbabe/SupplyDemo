# Context 模块：消息、工作状态与模型请求准备

F52 C1 来源/工作状态已实现并通过克隆验证；C2 已接通配置控制的 Worker/Pi/Gateway v3 路径，默认关闭；C3 已提供保守前缀摘要及受控渐进读取。下文尚未注明已实现的接口属于目标设计。开发入口 [F52](../../contracts/F52.md)；业务规则 [BL13](../../business-logic.md#bl13-短期状态context-装配与压缩f52-目标)；字段/时序 [DC18/DC21](../../data-contract.md#dc18-会话消息工作状态与提交水位f52-目标)；IPC 见 [Context 协议](../../pi_sidecar/CONTEXT_PROTOCOL.md)。长期记忆加工不在本模块，见 [Memory](MEMORY_RUNTIME.md)。

## Module 与 Interface

| Module / 目标位置 | Interface | 调用者不必知道的实现 |
|---|---|---|
| ConversationStore / context_store.py | commit_turn(conversation, expected_watermark, unit)、close、read_range、checkpoint | 幂等提交、完整消息关联、并发游标、来源提交、对象权限与 generation |
| WorkingState / context_runtime.py | accept_delta(expected_version, validated_delta)、rebuild(run, current_scope) | 结构校验、已接受引用、恢复时重验、模型建议不成为权限/数值事实 |
| ContextCompiler / context_runtime.py | prepare(request_context, transcript_refs, model_profile) → PreparedContext | 取舍、预算、来源/记忆重验、配对、压缩、snapshot |
| ContextReader / context_runtime.py | read(ref, level, page, read_context) | L0/L1/L2、分页、来源权限与成本；引用是 ID/版本，不是文件路径 |
| MemoryPort | recall(query, trusted_scope)、read(ref, level) | 由 F48 提供；空实现显式 disabled/unavailable，F52 可单独验收 |

context_store.py 已实现 C1；context_runtime.py、context_protocol.py、context_mapping.py、context_session.py 实现 C2；WorkingState 实际在 context_store.py，通过 checkpoint/rebuild 追加与恢复；accept_delta 目标通过已有 checkpoint 接受；ContextReader 在 context_compaction.py 实现。harness.py 保留 AgentEnginePort；PiRuntimeAdapter 调 Compiler，不引用 Mem0；intelligence.py 的输入启动快照继续保留。runtime.py/worker_main.py 提供 Job/取消；gateway.py 保存最终私有 request 并校验 snapshot 绑定。specialist_runner.py 只拿当前角色的授权消息与 Working State，不共享整个私人会话。

```text
ConversationStore / 领域证据 / WorkingState / MemoryPort
                         ↓
                   ContextCompiler
                         ↓ 私有 snapshot + 有界消息投影
                 PiRuntimeAdapter ⇄ Pi
                         ↓ 每次请求最终检查
                     Gateway → Provider
```

## commit 与恢复

既有问答默认独立 Conversation。显式续接必须验证同组织、原用户/允许参与主体及当前对象范围，不以 conversation_id 作为权限。原始消息顺序和调用身份由服务端接受，模型不能自行声明可信作者。

commit_turn 接受完整用户轮次及已接受产物，锁定 conversation watermark；同幂等键同内容返回原提交，不同内容拒绝。消息大正文留受控 payload/原产物引用，不能把未授权全文写入公开 Trace。关闭/任务终态提交尾游标和 outbox，不等待 Mem0。

Working State 只接收验证过的目标/约束/步骤、accepted_evidence_refs、unknowns/conflicts、失败/人工等待及 next_step；任何模型数值/状态必须由领域来源支持。版本追加且按活动 owner/generation/期望版本接受；恢复不执行旧半段工具调用，重新检查来源和 scope，建立新 invocation 的合法消息尾部。

## 每次 prepare 算法

1. 验证 Job/原用户/对象、实际 model profile 和请求身份；预留本次 modelCallKey。先处理 F51 未结批次，不能截断活动工具调用。
2. 加载当前任务、已接受 working checkpoint、最近完整消息单元、当前授权证据。按任务意图取 MemoryPort 候选，缓存命中仍重验；缺失状态保留。
3. 建立强制保留集合：受信规则、本次目标/范围、关键证据/否定/未知、活动配对。工具 Schema 与 Provider 协议开销计入预算。
4. 按角色分区装配；去除无关重复/过期块，旧闭合工具正文换有界投影/引用。每次按完整消息单元或记忆块裁剪，不能断掉工具配对或时间/否定条件。
5. C3 开始支持已闭合前缀的分块摘要，保留覆盖范围/hash、证据/未知、约束和下一步。摘要检查引用与游标；失败保留原记录。强制内容仍超限时报 context_overflow，不能清空头部任务。
6. 持久保存 snapshot 和逐块取舍 manifest，返回投影/模型政策/epoch/hash；临近 Provider 发送再次校验。与实际 request 序列化记录关联，不把两种 hash 混成一个。

## 配对、预算与配置

assistant 工具消息与其全部终态结果是不可拆单元；并发完成顺序仍按原 callId/index。活动批次不压缩。中断状态保持在 Transcript，不能为恢复伪造成功消息；新 invocation 用 checkpoint/当前事实重建。

已新增 config/product/context-policy.json：enabled=false、inputCap、reasoningReserve、安全余量、分区、缩减/摘要阈值、传输上限；实际 profile 由服务端读取。摘要调用与读取配置由 C3 补齐。ModelPolicy 实际值由 Gateway profile 提供，不用厂商宣传最大窗口。

初始 fixture 配置：输入目标上限 24,576 tokens；同时受实际 model window − output/reasoning −协议/工具开销−10% window 安全余量限制。各区软配额为角色/规则 12%、任务/绑定 18%、Working State 12%、近期消息 18%、当前证据 30%、长期记忆 10%，空余额可借出。硬约束优先于比例，角色可配置调整，配置不合计 100% 拒绝加载。

有效输入预算 80% 启动确定性缩减、90% 启动摘要、摘要目标 60%；C2 没有摘要时在确定性处理仍超预算后明确拒绝。无可用/校准 tokenizer 时不能以字符数冒充精确 token；采用经 fixture 验证的保守估算并记录方法，最终不得发送已知超限请求。

这些是当前保守验收配置，不构成质量/延迟承诺。大窗口模型不能越过部署 input cap 与成本政策。新工具 Schema、模型切换和大结果到达后都重算。

## 渐进读取与消融

L0 返回类型/实体/摘要/来源/尺寸，L1 返回有界完整记忆/episode，L2 返回原始授权片段。读时重验、按完整语义片段分页，登记字节/token 和来源版本；一次 read 的预算由当前 invocation 扣减。读取门禁属于内部 Interface，本期不自动新增模型工具；将来开放工具须同步 F51 Binding/Schema 与验收。

旧摘要引用来源撤权/删除时整块失效或重新生成，不能只清引用而保留泄露正文。compaction coverage 和 extraction cursor 独立维护；Context 摘要从不自动确认为长期 memory。

可关闭 MemoryPort、摘要模型或新 Conversation 入口分别消融。现有入口保持旧协议兼容，但已启动 v3 请求不得失败后无记录重发旧协议。具体配对、恢复、双模型协议、预算及来源失效测试由 F52 维护。


## C2 持久投影与发送

ContextSession.start 固定组织/原用户/Job/generation/角色，默认独立会话分区；每次 prepare 先提交规范化完整来源，再重验先前工具来源、计算投影。context_snapshot 保存模型身份、提交水位、source_epoch、Context/Model policy、投影/hash、manifest 和预算。原始消息与有损投影分别保存；模型反馈修正产生新调用，不重执行原调用。

Gateway 对锁定 Pi 的 Responses/GLM 文本及工具映射做验证，不生成 Provider body；原 callId 保留在 Transcript，Provider ID 的规范化只按已核对的 SDK 规则校验。projectionHash 使用 PostgreSQL JSONB 正文 hash；requestHash 使用最终有效 Provider request 的规范 JSON hash；分块 sha256 是传输 bytes hash，三者职责不同。accepted 与 model_exchange 插入同事务，不允许事后改写运行中的 request。

source_epoch 当前按组织保守失效；原用户/Job lease/scope 在接受时再次核验。context_required 不受私有历史 RLS 隐藏影响，撤权不能通过移除 v3 header 绕过准备。未发送的 prepared 快照在受控退出时取消；进程被硬杀后仍由 lease/generation/epoch 阻止旧消费，不宣称外发请求可撤回。

C2 为空 MemoryPort，仅记录 disabled，无长期记忆内容注入。分区软目标可借用空闲额度；当前缺少 C3 摘要/渐进读时，不为满足软比例丢弃任务、未知或最新完整证据。大工具批次 JSONL 超限时只投递原持久引用，ContextSession 从已接受结果恢复正文后重新计算模型预算。


## C3 当前实现与限制

ContextCompiler 在确定性工具正文缩减后仍达 summaryRatio 时，调用 context_compaction 的逐字段落去重摘要器。首部任务/最新完整单元/Working State 原样保留；只替换中间闭合区间。原文、引用、未知、条件和否定不由模型重写；无安全缩减或摘要器失败时保留原始来源，可容纳则继续，否则显式溢出。尚无外部 LLM 摘要配置，不据此声称抽象摘要质量。

ContextSession 在准备前冻结 epoch，在同一来源/快照事务中保存 compaction；0034 独立 compaction 列包含 sourceManifest/coverageHash/epoch，不污染 WorkingState schema。读取已失效摘要直接拒绝，不能删除引用后继续返回正文。ContextReader 的 allowance 绑定本次调用对象，重启使用新 invocation，不从失效 lease 继续使用旧 ready。

L0/L1/L2 是 Python 内部受控读取，不新增任意文件/模型工具。原始 Transcript 按完整消息单元分页，证据按完整字段分页；单个不可分片段过大时明确报错，调用方需在总读取预算内显式提高 page_bytes。预算、bytes 和估算方法均可审计；没有将长期提取水位当 compaction 覆盖水位。生产启用前须应用0032–0034，默认配置仍关闭。
