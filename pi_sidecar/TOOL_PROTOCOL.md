# Pi 工具批次桥接协议（F51 v2 已实现待验收）

修改 sidecar、PiRuntimeAdapter 或工具结果消息时读取本文。生产 Worker 固定 JSONL v2，Pi 保留 sequential 消费 Python 结果；显式 protocol_version=1 只作旧路径兼容回归。Python 入口 tool_bridge.py，调度 tool_scheduler.py；没有同时启用 Pi parallel。业务/数据权威见 [BL12](../business-logic.md#bl12-工具绑定执行与失败策略f51-目标)、[DC15–DC17](../data-contract.md#dc15-工具定义绑定与执行身份f51-目标)，验收见 [F51](../contracts/F51.md)。

## 固定依据与调度所有权

本地已核对 `package.json` 锁定 Pi Core/AI 0.73.1；其 `agent-loop.js` 在 assistant `message_end` 发出完整消息后，再从该消息提取 toolCall 并执行。`AgentOptions.convertToLlm` 支持异步消息投影。实施测试使用公开 `Agent.subscribe`、tool.execute 和 convertToLlm，不导入 dist 私有函数，不修改 node_modules、不维护 fork。

Python 独占领域调用的准备、分组、容量与执行。Pi 保留消息循环；`toolExecution: sequential` 可以继续用于按原位置消费已提交批次的缓存结果，不能把 Pi sequential 当成 Python 串行准入。Pi 不根据风险、资源或安全组重排调用。`tool_execution_start` 仅为消费事件，不再触发 Python 执行请求。

## 批次封口与桥接流程

1. assistant `message_end` 且消息成功完成、有 toolCall 时，按 content 中 toolCall 的出现顺序提取完整数组。文本/thinking 不发送；流式 partial/toolcall_delta 不形成批次。遇 error/aborted 消息不派发其中调用。
2. sidecar 在同一同步事件处理段先创建全部 callId 的 deferred/result 槽位和批次身份，再发送**一条完整** `tool.batch`。不等待 tool.execute，不以超时/静默间隔推断结束；每条 assistant 工具消息只有一个 batch。即使后续 Pi 在某调用本地校验失败，Python 仍收到原始完整批次。
3. Python 校验完整性并逐项 prepare，再 Scheduler.run_batch。结果先持久化后发回一个 `tool.batch.result`；数组覆盖全部位置，不能只回成功项。硬终止由 Python 记录失败/取消并关闭 sidecar，取消整个活动批次，不回普通业务结果。
4. Pi tool.execute 仅按不透明原 callId 等待 Python 缓存；禁止调用 Adapter、重新提交或网络重试。未绑定/非法参数可能被 Pi 原生准备过程直接拒绝，不能等待“每个 execute 都进入”作为批次完成条件。
5. 下一次模型调用的异步 convertToLlm 必须先等待该批次的 Python 结果/终止；按 callId 将 Pi toolResult 的内容与 isError 归一为 Python 的受限 ToolOutcome（包括 Pi 本地直接拒绝项）。投影只针对已记录批次，不修改调用参数、身份或业务数据；保留 Responses 原始复合 ID。这样本地 Pi 校验文本不会泄漏或覆盖服务端错误语义。未知工具名只精确映射 Binding 子集，保留原名供 Python 分类，不导入全局目录。
6. 投影检查每个原调用恰好一个结果、顺序一致后，才允许下轮 Provider 请求。最终 turn.completed 前也必须确认无未结批次。取消/超时/进程退出统一拒绝并清理全部 deferred；迟到结果不得重新唤醒模型。

这一流程允许 Python 在 Pi 等待 A 的缓存时执行 B/C，同时保留 Python 的 A→[B,C]→D 屏障。禁止只把 Pi 改成 parallel 或等待所有 tool_execution_start：前者产生双重调度，后者在现有 sequential 下会死锁。

## v2 消息形状

所有消息带 protocolVersion=2、turnId（本次 invocation）、递增 seq；每方向独立连续计数。继续使用 F42 的敏感字段/大小/未知字段失败关闭规则；v2 明确增加以下字段，v1 不接受它们。

| 消息 | 必需内容 |
|---|---|
| turn.start（Python→Node） | 既有受限上下文；toolProtocol=`python-batch-v2`；Binding 的版本/hash；tools[] 的 name、wireName、description、inputSchema、specVersion。只传绑定子集，不传执行 handler、权限凭证、资源键或完整用户身份 |
| turn.started（Node→Python） | engine、toolProtocol=`python-batch-v2`；缺失/不匹配时在任何调用前终止 |
| tool.batch（Node→Python） | batchId（turn 内从 1 递增的序号）、modelCallKey（Replay 为 null）、calls[]；每项 index（从 0 连续）、callId、name（解析出的业务名或原始未知名）、arguments |
| tool.batch.result（Python→Node） | batchId、results[]；每项 index、callId、status、code、modelPayload、resultRef（无业务证据则 null） |
| turn.cancel / turn.cancelled / turn.failed | cancel 为 Python→Node；cancelled/failed 为 Node→Python 并含稳定 code。Python 本地硬失败记 Trace 后终止进程；不新增双向 failed 命令。终止后禁止下一轮模型调用 |

callId 在整个 invocation 唯一，不截断或自行拆分 Provider 的复合 ID。modelCallKey 对应实际模型请求，不能根据收到工具事件时的全局计数猜测；在 streamFn 启动本次模型请求时捕获并关联最终消息。Replay 使用独立 batchId，不能伪造 model_exchange。

复用当前 Harness 的 `MAX_JSONL_BYTES=16_384` 作为每条完整 JSONL 消息（含 envelope）的字节上限；完整批次请求和批次结果都受此限制；请求超限在任何工具执行前失败，不拆成多条未封口调用。结果大小只能在执行并持久化后确认，超限终止 turn、不交付或截断，已保存结果仍可追溯，不能重执行来缩小输出。单批最多 16 项，turn 累计最多 16 项，非法/拒绝项计入；两端及测试共享/断言该上限，单结果也不能使批次结果超限。业务载荷过大应由 Adapter 返回有界投影和持久引用，不任意截断 JSON。

## 兼容与失败关闭

P1 允许 v1 tools 投影新增 description/inputSchema/specVersion，双方同步发布，保持原单调用执行语义；这不是 v2 批次开放。P2 新的可修正错误在 v1 无法安全表达时继续 turn 失败，不提前模拟 v2 行为。P4 的协议版本由 Python 固定选择 v2，并检验 turn.started；不做运行中自动协商或静默降级。保留 v1 回归夹具证明旧模式串行可用；业务切回串行使用 v2 Scheduler 策略，不改协议或绕过持久化。

缺失/重复/乱序 batchId、重复 callId、index 不连续、结果缺项/身份不匹配、活动批次未结束时发新批次、失败后发完成均为协议错误。未知名称/参数错误属于语义拒绝，不与损坏 envelope 混同。准备阶段发现撤权或失租时整批终止；服务暂不可用仍按位置反馈，是否继续业务由 Mission 决定。

## 必须证明的桥接案例

真实锁定 Pi + 无外部 Provider：一条 assistant 消息三调用、多个批次、混合合法/非法参数/未知工具、全部被 Pi 本地拒绝、缓存早于 execute 完成、执行逆序完成、结果归位、取消、失租和进程退出。断言每个合法调用最多执行一次，下一次模型请求只发生在全部结果持久接受之后，且只看到 Python 受限错误。Responses/Chat Completions 均覆盖原调用 ID 和模型交换关联。任一公共接口不足以完成上述证明，P4 停止，保留 v1 串行路径，不能用私有 fork 补洞。


## 当前验证与限制

真实锁定 Pi 子进程已覆盖 Responses/GLM 同消息三调用、串行消融/连续并行、乱序完成归位、Pi 本地拒绝后新 ID 修正、取消与超限。业务克隆验证在一次供应证据批次保存三个 Observation/台账，模型下一请求引用对应持久结果；另覆盖结果超限已保存但不交付。v1 保留已有 Harness/Gateway 回归；精确数目见 F51。

没有放大 JSONL 上限或添加工具分块协议；不能据此宣称任意大小的批次都成功。Pi Context 的长会话压缩/渐进读取仍按 F52 目标实施。
