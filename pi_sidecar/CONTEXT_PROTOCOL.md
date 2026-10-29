# Context 请求准备与分块协议（F52 v3）

F52 C2 已实现受配置控制的 v3，默认关闭；未启用 Context 时继续 F51 v2，v1 仅显式兼容。开发/验收见 [F52](../contracts/F52.md)，数据见 [DC18/DC21](../data-contract.md#dc18-会话消息工作状态与提交水位f52-目标)，工具批次语义继承 [F51 协议](TOOL_PROTOCOL.md)，预算/装配算法只在 [Python Context 模块](../src/product_api/CONTEXT_RUNTIME.md) 维护。

## 所有权与版本

v3 保留 v2 的 tool.batch、tool.batch.result、原 callId/index 和 Python Scheduler 所有权，新增 contextProtocol=1。由 Python 在 turn.start 固定选择 protocolVersion=3，Node turn.started 明确回显；失败不得静默降级。旧 v1/v2 仅用于未启用 Context 的既有入口/回归。

Python 管消息持久事实、装配、检索、摘要、授权与预算；Pi 保留消息/工具循环。只使用锁定 Pi 公开 async convertToLlm/transformContext、subscribe 和 streamFn；不调用上游私有函数、不开放 SQL/文件/Shell。Node 不持有 Mem0 客户端或 Provider 原始 Key。

## 每次模型请求状态机

idle → collecting → preparing → ready → submitting → idle；任何阶段可 cancelled/failed；Python 失败终止 turn，私有原因只作为安全 code 记录，不发送半成品输出。一个 invocation 同时最多一个 Context 请求；tool batch 必须结清后才开始下一次 collecting。所有消息沿用 turnId、每方向连续 seq 和未知字段/敏感字段拒绝规则。

1. convertToLlm 进入时预留本次 modelCallKey（invocation + 单调 ordinal），冻结要准备的完整消息列表。不能等待 streamFn 开始后才决定 key；即使取消导致未实际调用 Provider，也保留该 ordinal 的取消事实，不伪造 model_exchange。
2. Node 将完整的规范化消息列表以 context.input 分块交给 Python；当前每请求发送完整历史并受总量限制，增量 messageRef 尚未优化。Python 验证原 callId、角色、消息顺序及此前工具结果，作为私有 Transcript 接受，不信任 Node 自报的授权/scope。
3. Python 持久来源，运行 Compiler，发回 context.output 分块；末尾 context.ready 绑定 snapshotId/projectionHash、modelCallKey、policyHash、epoch。Node 完整校验后解析投影，只供本次 convertToLlm 返回。
4. streamFn 必须消费同一次 ready，不能再次加历史/记忆、修改工具集合或跳过准备；携 snapshotId/projectionHash 和原 call key 送现有 Gateway。下一次请求重新准备，不复用 ready。
5. Gateway 在发送 Provider 前校验 snapshot、当前 Job/原用户/scope/epoch、model policy 和调用身份；用锁定 Pi 的两种已支持协议验证允许的消息/工具投影映射。实际 Provider body 另记 requestHash，与 projectionHash 区分。不同 hash 不应冒称字节完全相同；内容未经允许变化时拒绝。ready 只能被一个受控发送接受，未知网络结果走既有计量/失败，不盲重发。

F51 convertToLlm 的工具结果归一必须先完成，后进入 Context prepare；只有一个组合 wrapper，不能让两套回调互相等待。Pi 本地准备拒绝也必须按 F51 生成安全结果，再纳入快照。

## 分块形状与上限

输入和输出使用同一 framing，direction=input/output；输入生产者 Node，输出生产者 Python。JSONL 每行仍最多 16,384 UTF-8 bytes（含 envelope），不是 16K tokens。body 先编码 UTF-8，再按 bytes 切为 base64 data，避免半个多字节字符；所有 content 均为本次授权消息数据，不接受任意 URI/文件路径。

| type | 字段（除公共 envelope） |
|---|---|
| context.input.begin / context.output.begin | requestId、modelCallKey、contextProtocol=1、schemaVersion、bytes、chunkCount、sha256；output 另含 snapshotId |
| context.input.chunk / context.output.chunk | requestId、index（0 起连续）、data（base64） |
| context.input.end / context.output.end | requestId、bytes、sha256 |
| context.ready | requestId、snapshotId、modelCallKey、projectionHash、policyHash、epoch、预算摘要 |
| context.final.begin/chunk/end | 与 input 同形，准备结束时提交原始完整历史；只接受当前 modelCallKey |
| context.committed | requestId；尾部消息、Working State 和关闭命令成功后确认，之后才可 turn.completed |
| context.local.consume / context.local.accepted | 仅 Replay：requestId、snapshotId/modelCallKey/projectionHash；Python 重验并消费后确认，不产生 Provider exchange |

初始验收 profile 总输入/输出各最多 1,048,576 解码 bytes，每块最多 8,192 解码 bytes、每方向最多 128 块；begin 的 bytes/chunkCount 提前校验且累计不得越界，控制消息不能夹带额外正文。Python turn.start 提供经验证的更低运行上限，Node 不得扩大。上述是传输上限，Compiler token/cost 限制独立执行；不能因为字节通过就绕过模型窗口。

接收方完整验证 count、index、bytes、hash、UTF-8 和有限 JSON schema 后才提交/消费；缺块、重块、额外块、不同 key、超限与第二活动请求均失败关闭，不拼接半段到模型。发送方保持原始来源；中断接收区是临时内存，取消/退出清理，不能留下可被下次请求复用的半成品。

## 失败、恢复与验证

等待受当前 Job deadline/取消约束，复用 turn timeout，不设无限等待；准备过程中失租/撤权/epoch 变化则终止本路径。Gateway 最终检查只能阻止此后未发送的请求，不能撤回已经外发的内容，不扩大为通用即时撤销保证。

硬退出后 SQL checkpoint/已接受 Transcript 为恢复来源，新 invocation 从完整消息单元重建；Pi 内存不是 checkpoint。模型/摘要服务不可用时 Python 可做确定性缩减；仍不合法就 context_overflow/summary_unavailable，Node 不自行截头或重试 Provider。

验收必须使用真实锁定 Pi：两次以上模型请求、合法多工具批次与本地拒绝、大中文/多字节消息、16KB 边界、最大总量、乱序缺块、取消/失租、模型窗口改变、ready 重用/跨 invocation/修改内容拒绝、最终 Gateway body 与 snapshot 映射、无记忆/摘要失败。两个 Provider 协议均用固定报文，不需要真实模型。任一公开接口无法兑现本状态机，停止 C2 并保留旧模式，不能用私有 fork 绕过。


## 当前实现边界

Python framing/Compiler 在 context_protocol.py/context_runtime.py；会话接线在 context_session.py/tool_bridge.py。Node framing 独立在 context_protocol.mjs，只有一个活动传输；等待受整个 turn deadline/取消约束。源记录不因模型投影缩减而被覆盖。未发送 ready 在退出时尝试取消；清理不可用会记录 context_cleanup_failed，租约门禁仍保持。

streamFn 使用 Python 返回的 messages/systemPrompt 与固定 Binding 工具集合，原生 SDK 保留序列化所有权；实际 body 在 Gateway 关联 snapshot 校验后入库。当前只支持已验证的文本/工具映射，其他媒体或未知内容失败关闭。C3 摘要与渐进读取尚未实现，不把 v3 分块传输当作无限窗口。
