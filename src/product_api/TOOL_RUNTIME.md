# Python 工具模块（F51 P1–P4 已实现待验收）

修改工具定义/绑定/执行/调度时读取本文；Pi 批次交接读 [协议](../../pi_sidecar/TOOL_PROTOCOL.md)。范围与验收读 [F51](../../contracts/F51.md)，业务策略只在 [BL12](../../business-logic.md#bl12-工具绑定执行与失败策略f51-目标)，数据只在 [DC15–DC17](../../data-contract.md#dc15-工具定义绑定与执行身份f51-目标) 维护。

## 当前源码与迁移落点

| 源码 | 已实现职责 |
|---|---|
| `tool_registry.py` | ToolSpec/ToolResource、严格 Schema、精确别名、Binding 指纹/受限投影；只读和 overlap 独立 |
| `model_tools.py` | 从 Registry 生成的不可变兼容别名导出 |
| `tool_runtime.py` | prepare/execute、不可变参数/单次消费、稳定错误、原用户来源重验、SQL 调用台账和结果接受；MemoryExecutionStore 仅独立夹具 |
| `tool_scheduler.py` | 完整批次准备、serial/contiguous、双方兼容、屏障/取消/截止、SQL 容量与续租/隔离 |
| `harness.py` / `tool_bridge.py` | v1 显式兼容；v2 封口/序号/预算/字节限制、调用身份和有序结果；生产 Worker 固定 v2 |
| `runtime.py` / `intelligence.py` | 保留领域 Adapter 与 Artifact/Observation；接受事务登记引用，传递 execution 身份；Worker 回收 abandoned/隔离占用 |
| `specialist_runner.py` | 校验角色工具集合完全一致；不接管底层调度 |
| `gateway.py` / sidecar | Gateway 管模型费用/交换；sidecar 消息循环使用 Binding Schema，工具调用由 Python 独占调度 |

0030/0031 未应用业务库，部署步骤见 F51。没有新增工具微服务或依赖，不恢复已删除 `src/tools`。

## 接口与所有权

```text
Mission 编排器 → Specialist / AgentEnginePort → Pi 消息循环
                                             │ 完整批次
                                             ▼
Registry.bind → Binding → Runtime.prepare → Scheduler.run_batch
                                             │ 每次执行
                                             ▼
                                        Runtime.execute
                                             │
                                      领域 Adapter
                                             │
                                ResultStore → Artifact / Observation
                                             │ 持久结果引用
                                             └────────────→ Pi
```

| 模块 Interface | 调用者需要知道的约定 | 隐藏的实现 |
|---|---|---|
| Registry.bind(names) → Binding | 业务服务先决定允许集合，仅绑定服务端已注册 names；返回有限集合及版本 | 精确解析、别名冲突检查、Schema/描述投影、可用性 |
| Runtime.prepare(identity, name, arguments, policy) → PreparedCall 或受限拒绝 | 无领域副作用；冻结有效参数与声明；硬终止异常直接上抛 | schema 验证、范围/权限预检、调度属性计算 |
| Runtime.execute(prepared, cancel) → ToolOutcome | 只执行当前 Binding 产生且未消费的 PreparedCall；产物先持久化 | 临近执行/接受时重验、Adapter 调用、ResultStore、稳定错误 |
| Scheduler.run_batch(raw_calls, runtime, cancel, deadline) → ordered outcomes | 完整批次、连续原位置；先准备全批，再按策略执行 | 屏障、兼容组、容量等待、取消传播、归位 |

P1 使用现有 Pydantic 依赖为工具参数定义 strict、extra=forbid 的输入模型，导出 JSON Schema，并保留领域 validator 的额外业务约束；日期/UUID 等转换留在显式 Adapter。Python 验证与模型展示使用同一声明，sidecar 不再按工具名分支手写 TypeBox Schema。不得为兼容 Pi 的参数强制转换而放宽 Python 门禁。

PreparedCall 是进程内私有不可变值，含深拷贝的参数、binding/spec 版本及调度属性；不能接受 Node 序列化“已准备调用”。变更参数必须重新 prepare，本期转换只发生在显式领域 Adapter 中。Context 包含组织、原用户、Worker、Mission/Run/Job/generation/invocation、绑定商机/Case/BOM/分析时点、取消/截止、授权检查、审计/ResultStore 和受控 Adapter；不把 Python 可调用对象投影给模型，不默认赋予工作目录/Shell/任意文件/通用 Hook。

## 调度算法

1. 完整校验批次身份/数量/位置并占用调用预算。逐位置 prepare；可修正拒绝保留该槽位，硬终止拒绝使全批停止且不启动领域调用。准备不代表锁住未来权限。
2. 串行策略始终逐项执行。分组策略从原位置向后扫描：未显式允许重叠、准备拒绝、内部写入/外部动作声明均作为本期屏障。只有连续且与组内每个调用均兼容的 PreparedCall 可入同组；第一个不兼容项封组，不跨过它找后续调用。
3. 组间完整等待，组内并发；并发之前按 DC17 原子取得该调用的全部资源，不持有 SQL 事务跨网络。资源声明只规定容量；互斥连接必须同时在兼容性声明中禁止重叠。不同调用不共享 psycopg connection/transaction。
4. 准入等待期间响应取消/截止和 lease 失效；取得准入后 Runtime 再验权限、绑定与 lease。执行后接受记录前再验；过期 generation 的完成不送模型，也不作为后序结果。
5. 组内可恢复错误占位；硬终止使等待和在途兄弟取消，不启动后续组。取消是协作请求，不代表远端已经停止。等待清理后再释放占用；未确认停止时按 DC17 隔离。
6. 按原 index 交付结果，不能按完成时间排序。单 Invocation 只接受一个活动批次。同 Trace 写入复用现有序列化 sink，业务数据库写入不能用 Scheduler 并发直接打破既有顺序保护。

兼容性计算使用服务端声明的对称检查：双方显式允许 overlap，且两边基于有效参数的兼容判定均为真，才可同组；任一缺省、互斥连接或冲突资源声明都判为不兼容。判定只能计算属性，不执行 I/O 或修改参数；资源容量准入另行处理，不能反向推导授权。

例：A（屏障）→ B/C（相互兼容）→ D（屏障）；实际 A→[B,C]→D，返回 A/B/C/D。B 与 C 有数据依赖时拆成两轮或由 Mission 建立前序，不在本模块推导依赖图。容量 1 时可在组内退化串行，不能把 D 提前。

## 错误、预算与迁移

稳定错误分类和能否继续由 BL12 决定，ToolOutcome 字段/持久状态由 DC16 决定。准备错误也消耗调用预算；网络、模型修正、Job 重试只沿现有各层职责处理，Runtime/Scheduler 不新增重试。v2 的一次 turn 最多 16 个原始调用（含非法/拒绝调用），每条结果和批次总字节限制见 Pi 协议；无效名称不能触发注册表枚举或隐式工具替换。

F36 ResultStore 使用真实 Artifact 引用；F45 使用 intelligence_observe 与 DB hash；统一调用记录只关联它们，不复制完整 payload 或取代引用校验。共享逻辑移入 Runtime 后删除原重复包装，保留领域门禁；不要执行两次持久化。CURRENT_TOOL_CALL 过渡时由 Runtime 设置并在 finally reset；并行任务仍以显式上下文为权威，测试跨调用隔离。

Registry.execute 已委托串行 Runtime，保留 v1 回归。生产 PiRuntimeAdapter 固定 protocol_version=2；仅通过 config/product/tool-runtime-policy.json 的 strategy=serial 做调度消融，不静默降级协议。回退至串行策略不改变调用 ID、记录格式、权限或产物。逐项测试和验收命令由 F51 维护。


## 配置与证据边界

`SchedulerPolicy` 在创建 v2 Adapter 时冻结配置；policy snapshot 含 schemaVersion/strategy/configurationHash，F45 input_context 与各调用 scheduling_snapshot 记录实际策略。资源 name/scope/units 仅由 ToolSpec 定义，容量键为 `organization:name` / `deployment:name`，实际组织资源键由 SQL Job 对应组织生成。没有真实外部连接的当前七工具没有 resource 声明；不能把本地合成容量验证表述为已接入供应商限流。

`PostgresExecutionStore` 是生产实现：prepare、临近执行、接受、整批交付均查当前门禁；来源引用在接受与交付前按原用户验证。Artifact/Observation 中 execution 与台账 invocation/call/batch/index 一致才接受；返回 payload 必须匹配接受时的值。结果不重复落库，调用台账只保存元数据和引用。

资源 acquire 为全部资源的短事务原子操作，renew 不能复活过期/隔离占用；正常返回 clean release，取消/异常保守隔离。受控 release 需精确 owner token，只能在确认操作已停后使用；未提供自动销毁真实供应商 session 的运维能力，未知状态继续占用容量。Worker reap 会隔离过期占用，不自动释放。

每条完整 JSONL 消息上限 16384 字节，批次与 turn 都最多 16 个原始调用。请求超限先拒绝；结果超限在已持久化后终止交付，不拆批、不截断、不由 Runtime 重试。大业务正文仍需 Adapter 给出有界结果与引用，Context 渐进读取属于 F52。
