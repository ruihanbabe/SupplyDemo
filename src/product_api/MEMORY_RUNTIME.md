# Memory 模块：捕获、治理、Mem0 投影与遗忘

F48 M1–M3 已实现本地合成路径，最终验证结果见 Contract；用户验收与真实部署分开。修改长期记忆时先读 [F48](../../contracts/F48.md)，业务策略读 [BL09](../../business-logic.md#bl09-memory-选型与治理边界)，字段/状态读 [DC19/DC20](../../data-contract.md#dc19-记忆来源不可变版本与关系f48-目标)，上下文消费读 [Context 模块](CONTEXT_RUNTIME.md)。本文维护目标 Interface、实现落点与加工算法，当前限制见下节。

## 当前实现与边界

memory_store.py/0035 管来源、不可变版本、证据、独立租约工作项、投影和删除请求；0036 追加闭合增量范围与 summary_manifest，并更新 capture/guard/accept/indexed。memory_runtime.py 提供共用摘录器、单步 Worker 与 recall/read/correct/forget；mem0_adapter.py 校验固定制品并隔离本地实际 Mem0/Qdrant/SQLite；ContextSession 在显式 backend factory 下启用捕获与带 trust/来源的检索注入。配置默认关闭且仅支持 synthetic，本轮不改业务库。

summarize作为保留原顺序的确定性基线，不删除重复的最终纠正；启用的Worker另经LocalModelPort执行有来源约束的模型选择。capture 只读取新增水位，按完整 message_unit_id 打包，保守以存储行 JSON 6144 字节为分块目标；不能拆分的单元单独保留，由 Worker 的32KiB输入/8KiB输出门槛决定能否加工。store.source 读取精确起止范围并验证 hash；closed_units 复核连续性、complete、调用/结果配对；摘要同时保留 assistant 工具参数、失败和引用。SQL 接受时生成无正文的 coverage manifest，L1 返回该清单，召回引用带 startOrdinal。

同会话的独立区间均可保留 ready，乱序完成不抑制其他区间；memory_guard 只拒绝被已索引更完整范围包含的旧前缀，index 接受只退役被覆盖的旧范围。纠错不能恢复被遗忘、删除、过期或来源不可读的版本。实际后台入口要求显式 SUMMARY_URL，经独立 Memory 租约和0037计量调用；直接 Python 构造 Worker 不传 summary_endpoint 时保留确定性摘录作为消融。现有业务 Gateway capability 不被复用。

本地 erase 先原子 deny，再按隔离域销毁/重建 Mem0 存储，清 SQL 与消费依赖的 Context/派生版本，结果保留 backup_pending。0037/0038 的计量、依赖、取消、保留和恢复入口见下节。M2/M3实现见文末；可选Mem0 infer=True稳定候选抽取及生产有损语义巩固仍未开放。

## M1 模型与生命周期实现（0037/0038）

- `memory_models.py` 的 LocalModelPort 只接受回环HTTP `/v1`，固定合成模型、无代理/重定向、有限超时/输入/响应。摘要模型按同一提示选择自然语言段落索引；正文由原段落重建，拒绝虚构、乱序、重复索引和丢失用户/工具内容或限定语。它是有来源约束的抽取式模型摘要，不开放任意改写，也不声称真实模型业务质量或压缩收益已验证。
- SDK 默认 embedding 会丢弃 usage，Adapter 在固定制品边界注入 LocalModelPort。publish/search/rebuild 的每次实际 embedding 与摘要均先独立提交 memory_operation 预留，再发请求，最后结算；父发布事务失败也保留调用记录。每次最多65536单位、组织滚动一天1000000单位；fixture已知零价格才写 cost=0，缺usage或中断保留unknown/reserved与NULL费用并占预留，不假装零消耗。真实端点/价格/凭据政策不开放。
- 0037 增加 memory_operation；work/generation、source_epoch、namespace、policy/model/input_hash 和 usage 关联。Memory 租约与当前来源授权在外发前、接受前重验；业务Job终态不阻止后台，但不能用过期Memory租约调用。NO KEY UPDATE 保留租约串行，允许独立计量的FK key-share；domain锁序贯发布/依赖登记/纠错/删除。
- 0038 增加 memory_dependency、memory_recovery_state、memory_replay_log。snapshot准备事务验证版本/正文hash/原用户/精确对象域，登记version→consumer conversation。recall/read及再加工递归核验全部上游来源和真实证据；原始来源失效时派生不能继续使用。忘记收集声明类型的依赖闭包、取消旧工作项并关闭受影响会话，阻止迟到Provider结果重新落正文。0039/0041进一步加入显式关系与跨片段派生，见文末。
- asyncio取消会等待有界外部线程退出，SQL不接受取消结果；已产生远端写入的取消会登记hard清理。擦除中断保留requested，后续重试按SQL合法存活版本重建，不能以部分物理操作报完成。七天合成保留覆盖已发布/候选及未加工来源；Worker每步先执行到期deny/清理，不只是查询时隐藏过期版本。

恢复受控命令均只供Python运维调用，不对模型注册：`save_deletion_ledger(path)` 以0600临时文件、fsync、原子替换和目录fsync导出无正文日志并返回SHA256；日志需独立于可恢复备份保留且选择最新版本。恢复数据库/向量后，在开放流量前调用 `begin_recovery()`，再 `restore_deletion_ledger(path, expected_hash)`；文件/校验失败保持blocked。重放幂等登记deny/清理，Worker处理所有requested后，`finish_recovery()`才允许解除屏障。真实运维仍须保证最新日志来源和备份清单；这里不自动发现或删除未知外部备份，backup_pending如实保留。

## Module 与现有源码落点

| 目标 Module | Interface | 实现职责/现有落点 |
|---|---|---|
| MemoryService | capture(source_range)、confirm(candidate, actor)、correct(version, change)、forget(targets, mode)、recall(query, scope)、read(ref, level) | memory_runtime.py 已有 recall/read/correct/forget；捕获由SQL触发器，显式偏好和确认经 Store；模型不能任意指定 scope |
| MemoryStore | commit_capture、accept_version、claim_work、record_projection、deny/delete_progress | memory_store.py 已有对应受控命令；物理责任见 DC19/DC20 |
| Mem0Adapter | publish_version、extract_candidates（按需）、search、get、reconcile、erase | mem0_adapter.py 已有 publish/search/reconcile/erase_and_rebuild；候选抽取尚未实现；返回有界结果/稳定错误 |
| MemoryWorker | handle(work_id, lease) | 持久领取、抽取/发布/对账/删除/巩固；复用 runtime.py/worker_main.py 的 lease 模式，不在 HTTP 里跑 LLM |

旧memory.py及F36写入/确认命令继续保留。M2导出、状态映射、来源复核、投影对账与显式单读路由已在克隆验证；真实迁移/切换未执行，不能把旧confirmed直接映射成无需来源重验的新active。

Context 与 Memory 通过小 Interface 连接：recall 返回 MemoryBundle（版本、正文/有界摘要、来源、范围/时间、确认/冲突标记、状态与排除原因）；read 返回 L0 目录提示、L1 完整有界记忆或 L2 原始授权片段；所有调用携带可信 ReadContext。空后端明确 unavailable/disabled，不退回另一有效索引。

## 通用摘要与按需发布

1. source range 与工作项同 SQL 事务提交；按新增 watermark 领取。先做来源、权限与 PII/密钥检查，随后受控摘要器处理完整片段。按完整闭合消息单元分块，保留覆盖区间；不按角色/场景挑选要提取的业务字段。
2. 共用自然语言摘要提示词保留背景/进展、结论及理由、纠正/否定、时间/条件、未知与未决；SQL 校验来源/hash、准入与版本后登记 process_summary/ready/generated_reference。无需逐条人工审批，但不作为已确认事实。
3. 默认使用 Mem0 add(infer=False)，一条有界摘要版本对应一条投影；不再对摘要调用 infer=True 二次改写/拆事实。摘要模型、embedding 独立计量；记忆摘要从原始来源生成，不从唯一的 Context 有损摘要生成。
4. 显式偏好/稳定规则作为独立按需分支；若调用 Mem0 add(infer=True) 抽取稳定候选，candidate 与 published 使用独立 vector collection/history-message DB 和可信 namespace。每个抽取批次唯一 run_id，不把临时候选混入发布检索；不为每角色编写提取器。
5. 稳定候选走确认后追加 stable_memory/active；process_summary 走自动历史参考准入。两类均发布准确 SQL 版本，Mem0 返回正文/hash 不一致拒绝；状态/关系改变不改写旧正文。来源失效同时阻止两类使用。
6. recall/read 返回 trust、content_kind、准确版本与来源范围；Compiler 必须区分 generated_reference 与 confirmed，前者只能帮助回顾/找原文，不签发当前业务事实或授权。

Mem0 固定基线与 infer=False、按需候选隔离/去重、擦除限制见 [研究](../../docs/research/2026-10-27-memory-framework-selection.md)。实施时锁定可复现依赖制品并验证差异，不跟随 main。新默认路径避免候选提取/发布的重复 embedding；按需分支仍如实记录全部调用成本。

## 幂等、对账与失败

work dedupe key 覆盖 source range/hash、policyVersion、operation；publication_key 覆盖组织+memory version+backend epoch。SQL 锁住同版本发布；Mem0 写后超时先按 metadata publication_key 精确对账，不能盲重发。查到多个副本只选择与 SQL 内容一致的一份，记录并回收派生重复；未知结果留 reconciliation_required，不承诺 exactly-once。

取消、删除、来源失效在领取后、外发前、发布前和接受前检查。业务 Job 已结束不表示加工工作结束；Memory 工作项使用自己的有效 lease，保留 originating_job/generation 作因果，不借过期业务 Job 授权。确认等待不让业务 Run 一直 running。

错误类别：source_denied/source_invalid、candidate_rejected、backend_unavailable、write_outcome_unknown、budget_exceeded、erase_incomplete、internal_error。仅可恢复后端错误由工作项策略调度重试；授权/删除拒绝终止，未知写结果先对账，原始异常不进模型/公开 Trace。

## 检索实现

按 BL09 的意图选择器决定领域工具/知识/记忆/Working State。可信 namespace 与对象 filter 在后端预过滤；只允许过滤后的内容进入 reranker。返回后 hydrate SQL canonical version，重验当前 ACL/状态/来源，去重、冲突成组，再按 Context 预算选择完整块。

cache key 包含组织、原用户、角色、对象/scope、query、policy、memory/source/ACL epoch。缓存只复用候选，不能复用过去的授权；删除/撤权立即推进 epoch。按 ID 的 read 与 search 使用同一门禁，不允许渐进读取绕过过滤。

## 复盘、巩固与删除

复盘在任务终态提交通用过程摘要任务；巩固按安全域和输入 watermark 领取。归并只追加新版本与 derived_from/merged_from，提交时重验全部来源；Dream 不更新业务对象、能力或配置。失败重领仍使用输入区间幂等，不能把自己的旧结论当独立新证据。

erase 必须覆盖 candidate/active、history/messages/entity、SQL 正文和依赖的快照/摘要/缓存；根据 DC20 的每目标状态执行，不以 search 不命中替代擦除探针。固定 Mem0 delete 会留下旧正文历史，必须验证受控维护能力；没有完整擦除能力就不通过 M1。维护实现若需识别后端存储格式，必须限定到锁定制品并检查精确 schema，不能在升级后猜表清理或维护上游私有 fork。

## 配置与上线前置

现有config/product/memory-policy.json默认关闭Memory和Dream，仅允许synthetic Mem0与七天期限。Dream可在显式synthetic配置下按0041无损收益门槛开启；生产仍拒绝。目标仍需 summarizer/embedding/reranker policy refs、可选 extractor policy ref、candidate/active storage refs、摘要准入/稳定记忆确认规则、search limits、合批/预算与分类保留清理；未配置不能启用真实数据。敏感地址/凭证仅由部署配置引用，不写源文件。

初始离线评测参数：普通合批 15 秒/最长延期 60 秒；显式记住/纠错/关闭立即可领取；每意图候选 30、最多一次扩搜、注入条目上限 5；Dream 默认关闭，每日有新增时或每安全域累计 50 个新增 ready/active 版本触发候选任务。参数属于目标配置，不是索引完成 SLA。query 分解最多 3 个意图；无新材料不消耗巩固模型。

保留期限按数据类别由组织配置提供；未提供时真实数据准入关闭，合成 fixture 使用显式有限期限。不能继承框架默认永久保存。上线缺少后端/摘要/embedding/保留/预算配置即拒绝启用；按需提取分支未配置则关闭，不阻塞默认摘要路径。

提取与巩固模型经 Python 受控 ModelPort/Gateway；M1合成embedding经LocalModelPort及独立SQL预留/usage；生产embedding/reranker仍须新增明确授权/价格政策，现有业务Gateway没有通用embedding路由，不能据合成实现声称生产入口已开放。模型、embedding 的凭据只在 Python，Node 不持 Mem0 客户端。fixture 测试与真实调用授权分开；SDK 默认外部模型不启用。

## M2 实现入口（0039/0040）

MemoryGovernance提供发布前qualify、受控relate、describe、旧数据export_legacy/import_legacy/reconcile_legacy以及route管理命令。版本可选时间/条件由SQL保存；实体继续使用原业务scope ID，不按名称猜测。重复必须同域同文同trust/条件/时点，追加来源；冲突显式展示，supersedes退役旧投影，派生保留所有原证据。read/recall带历史参考边界。

旧数据桥先核knowledge版本/ACL/当前旧状态，来源持续动态复核；confirmed不直接绕过来源与投影门禁。route仅返回选中路径，不混合；新路径删除屏障在legacy回滚也有效，include_source硬删同时清原旧正文。缺来源、撤销或待重验记录保留原状态等待处理，不强制迁成active。未迁移业务数据，未改变现有Context默认关闭。

## M3实现入口（0041）

MemoryDream.schedule(scope, enabled=False)默认无动作；显式synthetic开启后SQL按原始支持指纹创建memory_dream_batch+source+work_item，复用MemoryWorker的claim/generation/lease/计量与发布。独立input集合由memory_derivation存储；同一SQL日内须至少50新增可用版本才再次排队，次日也须指纹变化且通过收益门槛。批次由memory-worker的显式步骤触发，不新建定时服务。固定lossless-dream-v1仅做精确块去重/拼接，保存时间/条件，≤30输入/8000字节；只有精确重复或>5槽位覆盖收益时排队。批次不调用独立LLM，仅正常embedding调用入账，不虚构额外模型费用。

0041的版本触发器仅将Dream输出标为process_summary/ready/generated_reference；propose_lesson必须原句引用，仍candidate。独立来源通过roots回溯，消费会话、Dream与经验候选不会按转述次数增支持。cold仅降低排序，保持相同治理存储/准入/L2能力。runtime_job终态关闭opt-in会话并及时提交尾批；Dream不改变业务对象。

删除inventory新增sources，包含尚无版本的派生副本；全部工作项先deny/cancel，hard再清副本。恢复可能为多assertion产生多个清理请求，必须处理全部requested后才能finish_recovery，不能只清第一个。源配置enabled=false、dreamEnabled=false保持；命令在Python私有控制面，没有模型侧写工具。启用仍须完整生产配置和运维备份清单，合成无损收益不能替代真实模型/排名/业务验收。
