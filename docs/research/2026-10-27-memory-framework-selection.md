# SupplyAgent：Mem0、MemOS、OpenViking、MemMachine 选型核验

> 核验日期：2026-10-27（用户时区 Asia/Shanghai）。通过 GitHub API 固定提交，再读取该提交的官方文档、源码和 LICENSE。只做源码/文档核验；未安装、部署、调用 LLM/embedding 或执行跨租户安全测试。本文是研究依据，不代表功能已实现或用户已验收。框架能力是事实，SupplyAgent 取舍明确标为建议/推断。没有用项目当前表的多少推导产品需求规模。

> 后续决策：用户已将 F48 默认路径调整为通用过程摘要 + infer=False 发布，事实抽取/候选确认仅按需使用。本文下述候选隔离路径保留为固定源码研究与可选分支，现行业务策略以 BL09/F48 为准。

## 比较边界与修订

“memos”在本文指 **MemTensor/MemOS**，不指同名轻量笔记项目 usememos/memos。四者不是相同层级：Mem0 是可嵌入记忆组件；MemOS 是范围更广的记忆管理服务；OpenViking 是包含资源、记忆和技能的上下文数据库；MemMachine 是 episodic/semantic 记忆服务。不要用总功能数、GitHub 星数或不同数据集的厂商分数直接判优。

| 对象 | 固定提交 | 提交时间 UTC | 主项目许可（当前固定版本） |
|---|---|---|---|
| Mem0 OSS | [`26cdefb46600c0fa5953c4b5ee5003c125539fc4`](https://github.com/mem0ai/mem0/tree/26cdefb46600c0fa5953c4b5ee5003c125539fc4) | 2026-10-26 14:51:39 | [Apache-2.0](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/LICENSE) |
| MemTensor/MemOS | [`adcb9f270c982f2b9ff8db9714792829d3bd6ab8`](https://github.com/MemTensor/MemOS/tree/adcb9f270c982f2b9ff8db9714792829d3bd6ab8) | 2026-10-23 12:02:14 | [Apache-2.0](https://github.com/MemTensor/MemOS/blob/adcb9f270c982f2b9ff8db9714792829d3bd6ab8/LICENSE) |
| OpenViking | [`2425cf41b9120bffdc83723afc9d8473a7794407`](https://github.com/volcengine/OpenViking/tree/2425cf41b9120bffdc83723afc9d8473a7794407) | 2026-10-27 02:29:06 | [AGPL-3.0 主项目](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/LICENSE)，部分 CLI/示例另有许可 |
| MemMachine | [`d5a39a8e774e977fb1e6c22efb03555c4e51e05e`](https://github.com/MemMachine/MemMachine/tree/d5a39a8e774e977fb1e6c22efb03555c4e51e05e) | 2026-10-12 23:23:24 | [Apache-2.0](https://github.com/MemMachine/MemMachine/blob/d5a39a8e774e977fb1e6c22efb03555c4e51e05e/LICENSE) |

OpenViking 的旧版 Apache 结论不能套到本次修订；本文记录许可文本，不对具体商业部署作法律结论。Mem0 OSS 与 Platform 托管 API 也不能互换：下面推荐按固定 **Python OSS** 接口讨论。

## 从业务能力比较，而非从现有表规模比较

| SupplyAgent 需要的能力 | Mem0 OSS | MemOS 服务 | OpenViking | MemMachine |
|---|---|---|---|---|
| 对话中提取事实、偏好、经验 | additive extraction + metadata | 多模态/工具记忆、MemCube、反馈纠错 | session 提取到可读 memory 文件 | episodic 原始消息 + semantic/profile 抽取 |
| 短期缓冲与摘要 | 本次未核实完整 context compiler；add 近期消息仅供抽取上下文 | 有管理、调度组件；不能把论文全能力等同生产路径 | Working Memory 可生成 overview/checkpoint，但当前默认关闭 | 明确有 short-term episodes 与 summaries |
| 原文与引用 | infer=False 可存原文，但不是工具 transcript 协议 | 图式记忆、来源与反馈能力需按部署验证 | 原始 session JSONL、URI、memory diff | episode store；semantic citation 指向 episode IDs |
| 渐进读取 | 应用自己做引用/按 ID 取回 | 检索/路由能力存在，应用仍管业务范围 | L0 abstract/L1 overview/L2 full content，URI 层级检索是强项 | 邻近 episode expansion、独立 semantic 检索 |
| ADD-only 自动抽取 | 当前 OSS 已支持；显式 update/delete 仍可用 | 默认反馈可纠错替换，不等于 ADD-only | 抽取可 add/update/delete 文件，不等于 ADD-only | 保留 episode 不等于 semantic 永不更新 |
| 有效版本、撤权、审批、PII hard erase | 应用补齐 | 不能以 Cube 或 Dream 替代产品治理 | account/user/ACL 不等于 SupplyAgent 对象权限 | org/project boundary 不等于 SupplyAgent 对象权限 |

对应事实来源：[Mem0 核心实现](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py)、[MemOS 服务能力及部署矩阵](https://github.com/MemTensor/MemOS/blob/adcb9f270c982f2b9ff8db9714792829d3bd6ab8/README.md)、[OpenViking context layers](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/docs/en/concepts/03-context-layers.md)、[MemMachine memory types](https://github.com/MemMachine/MemMachine/blob/d5a39a8e774e977fb1e6c22efb03555c4e51e05e/docs/open_source/memory_types.mdx)。表中“应用补齐”是边界判断，不表示这些框架只适合简单项目。

## Mem0：可直接使用的部分与必须包住的部分

### 已核实：add 是抽取加写入，不能当纯提取器

固定 OSS 的 `add(infer=True)` 是 V3 phased batch pipeline：按 user/agent/run 检索最近 10 条相关旧记忆，读取最近消息，用一次 LLM 调用抽取新增事实，批量 embedding，以正文 hash 去重，再写入记录；自动路径不会因冲突自动 UPDATE/DELETE。LLM 调用失败会抛出 LLMError，但解析失败/无抽取可返回空列表；空列表不证明知识完整。[实现 L881–1045](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L881)。

公开 `add` 没有经本次核验的 extract-only / approve-before-write 参数。因此把“抽取后再做 PII 或污染门槛”直接放在 active store 的 `add` 返回之后，已经太晚。`custom_instructions` 能约束提取目标，但不是安全隔离和校验器。[add 签名与执行入口](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L760)。

`infer=False` 对每个有效且非 system 的 message.content 创建一条记录，保留该 message.role/name，独立做 embedding；字符串会先标准化成消息。它不自动将整段对话压成事实、不执行相同的抽取去重段，也不保留完整 tool-call/result 协议语义；原文多条消息与一个整段字符串的入库粒度不同。[分支 L881–918](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L881)。

### 已核实：metadata、去重与权限有不同边界

1. **检索 filter**：search 将自定义 metadata filter 传给向量与关键词后端；PGVector 在同一 SQL 中使用 WHERE 再 ORDER BY distance/LIMIT，因此不是仅在 top-k 结果上做应用后过滤。复杂 operator 支持需逐后端测试。此事实不代表数据库 RLS、身份认证或对象授权已成立。[search](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L1642)、[PGVector SQL](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/vector_stores/pgvector.py#L331)。
2. **抽取内的旧记忆召回**：该路径只留下 user_id/agent_id/run_id 作为 search_filters；不会自动带入 org_id、case_id、status 等自定义 metadata。仅给 active/candidate 打 status 标签不能证明抽取上下文隔离。[L926](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L926)。
3. **同文不同来源**：infer=True 的 hash 仅由文本形成；已召回 top10 或本批中出现相同 hash，会跳过，未自动合并新来源。它是有限候选中的去重，不是全库唯一约束，也不能证明所有重复被排除。来源多对多关系必须另存，不能把一次 ADD 返回当完整 provenance。[L1008](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L1008)。
4. **显式更新**：当前 `update(memory_id, text=None, metadata=None, expiration_date=...)` 支持 metadata-only；user_id/agent_id/run_id/actor_id 不可通过 metadata 改写。API 按 ID 更新本身不代替调用者对象授权。[L1829](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L1829)。
5. **AsyncMemory** 是 awaitable 的并发执行接口，不能据其名称宣称存在产品级耐久异步提交队列；Mem0 Platform 的异步 event API 是另一服务能力。[AsyncMemory](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L2149)、[Platform API reference](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/skills/mem0/references/api-reference.md)。

### 建议：用候选隔离复用 Mem0 抽取，不复刻一套提取算法

以下为 **SupplyAgent 适配方案，尚未执行验证**：

```text
已持久化且经过入站 PII/密钥过滤的 source
  → durable outbox/job
  → 隔离的 Mem0 candidate 实例 add(infer=True)
  → 获取候选及源消息关联
  → SQL candidate：schema、敏感级别、来源、冲突与适用范围校验
  → 自动发布条件 / 必要时人工确认
  → active Mem0 add(infer=False)，每个批准 memory version 一条
  → SQL 登记外部 memory ID、版本、来源与投影状态
```

需要兑现的限制：

- candidate 与 active 使用独立 collection，并隔离 `history_db_path`。Mem0 在 add 路径保存消息与 history，隔离不能仅靠 status 字段；scope 由服务端编码 org、用户和精确业务对象，不接受模型自报身份。每次抽取 job 使用明确作用域；若用唯一 run_id 隔离每批，会失去跨批已有事实上下文，需 SQL 冲突检测补足。
- PII/密钥门槛要在向 candidate 及模型发送之前做一次，抽取后再校验一次。两阶段不能被“candidate 不展示给用户”替代。
- 候选 add 已产生查询 embedding、抽取 embedding，发布 infer=False 又做 embedding，并可能有实体抽取/检索成本；不能宣称整个过程总共只有两次调用。公开 add 未见批准后迁移已有 vector 且保持业务原子性的接口，本方案接受重复 embedding 的成本。
- infer=False 每次生成新 UUID，没有本次核实的业务 idempotency key。数据库 outbox 去重不能单独解决“远端写成功、本地登记前崩溃”；适配器必须写稳定 version/job metadata，处理未知结果及重复投影回收。不可承诺跨 SQL/Mem0 exactly-once 事务。
- 多条 source → 一条 memory 的来源集合保存在 SQL；candidate hash 去重可能让本批源映射不完整，不能只读 Mem0 ADD 结果推断来源。可按独立 job 抽取并关联整段来源，再在 SQL 确认具体证据范围。
- active metadata 可存 scope、version_id、lifecycle_state 等检索辅助字段；SQL 仍决定是否有效、是否可读和能否注入。更新正文遵循新版本 ADD，旧版本退役；允许显式更新非正文索引状态。这与“自动抽取 ADD-only”是两条不同规则。

上述方案尽量复用 Mem0 抽取/检索而把租户、安全、版本和来源交给产品领域层，依据是前述公开接口限制；不是已获得框架自身保证。

### 遗忘核验：delete 不等于 hard erase

Mem0 `_delete_memory` 删除向量后，会把旧正文写入 SQLite DELETE history；add 还可保留原始 messages，entity cleanup 异常被设计为 non-fatal。因此单调 delete 不能证明 PII 已硬删除。[删除实现 L2114](https://github.com/mem0ai/mem0/blob/26cdefb46600c0fa5953c4b5ee5003c125539fc4/mem0/memory/main.py#L2114)。

建议 soft forget 先在 SQL 禁止召回/注入，再异步更新/移除索引；hard delete 使用专门 erase job，覆盖原文、candidate、active、history/messages、entity 投影、缓存、日志和备份保留策略，并生成不含正文的删除证据。该流程尚需验证，不能把框架 TTL 或 delete_all 当完成证据。

## MemOS：范围广，但不要把 Dream beta 当产品级后台治理

官方主服务提供 MemCube、多模态/工具记忆、异步 MemScheduler、反馈纠错，默认 OSS 服务部署示例为 Neo4j + Qdrant。其同仓 local plugin 是另一套 SQLite、本地 agent 集成产品；不能拿 local plugin 的零设施成本代表 Python API 服务部署。[固定 README 的产品矩阵与启动说明](https://github.com/MemTensor/MemOS/blob/adcb9f270c982f2b9ff8db9714792829d3bd6ab8/README.md)。

Dream 是可选社区 beta、默认关闭：成功 add 产生信号，scheduler 执行 context/insight/diary 管线；但信号只在内存、不耐重启，自动触发主要按 pending-memory 阈值，冲突/反馈/碎片等只是扩展方向，update/merge/archive 长期策略尚非完整产品行为。[Dream README](https://github.com/MemTensor/MemOS/blob/adcb9f270c982f2b9ff8db9714792829d3bd6ab8/src/memos/dream/README.md)。

**推断**：若 SupplyAgent 明确需要图式知识、多模态材料统一推理、多 Agent 记忆调度，MemOS 值得做同业务样本对照；其能力更广不能自动抵消新图数据库、向量库、调度器及已有 SQL Job 系统的集成成本。当前没有证据要求把 Mission 编排和 context 所有权交给 MemOS。

## OpenViking：分层阅读与 session commit 很合适，整体接入跨度较大

OpenViking 把 resources/memories/skills 暴露为虚拟文件系统，以 L0 abstract、L1 overview、L2 full content 支持先读概要再读原文。检索区分直接 find 与结合 session 的 search；这适合复杂材料的渐进读取，但不是供应商报价或审批的业务事实权威。[context layers](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/docs/en/concepts/03-context-layers.md)、[retrieval](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/docs/en/concepts/07-retrieval.md)。

commit 的第一阶段同步持久化原始 JSONL 并入 durable queue，第二阶段异步生成和提取；commit 成功不等于记忆可检索。提取可 add/update/delete memory 文件，有 diff；consolidation 可以合并和删除原文件，故不能原样当 ADD-only ledger。[session](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/docs/en/concepts/08-session.md)、[consolidation](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/docs/en/context-compilation/06-memory-consolidation.md)。

**当前版本 Working Memory 默认关闭**，commit 仍保存原文并抽长期记忆，host 保持原生 history/compaction；Pi takeover 需显式开启，不能把实验适配当默认稳定行为。[WM 默认关闭及兼容说明](https://github.com/volcengine/OpenViking/blob/2425cf41b9120bffdc83723afc9d8473a7794407/docs/en/guides/working-memory-default-off.md)。

**推断**：可借鉴 URI、分层摘要、同步归档加后台提取；若未来 SupplyAgent 要统一大型文档知识库与 agent context，再评估整套引入。当前整体部署会与已有 Artifact/Trace/Context 边界重叠，并增加 AGPL 主项目的采用审查；不能只说“它提供了压缩所以免写 context compiler”。

## MemMachine：需要全文事件记忆时是最有意义的替代候选

MemMachine 将 episodic 分为 short-term（原始消息和摘要）与 long-term，并有 semantic/profile 记忆。原始 episode 存在独立 store；semantic feature 的 citation 指向 episode ID。文档有旧 history 表描述与新 Episode Store 说明共存，实施时应以固定源码迁移为准，不照抄旧表。[memory types](https://github.com/MemMachine/MemMachine/blob/d5a39a8e774e977fb1e6c22efb03555c4e51e05e/docs/open_source/memory_types.mdx)。

Memories API 以 org_id/project_id 命名范围，支持 metadata filter、邻近 episode 扩展；user metadata key 要带 m./metadata. 前缀，误写会 400。Project API 将项目描述为隔离边界，但仍需验证可信身份如何获得与绑定项目，不能将请求传入 org_id 当授权。[Memory API](https://github.com/MemMachine/MemMachine/blob/d5a39a8e774e977fb1e6c22efb03555c4e51e05e/docs/api_reference/python/memory_api.mdx)、[Project API](https://github.com/MemMachine/MemMachine/blob/d5a39a8e774e977fb1e6c22efb03555c4e51e05e/docs/api_reference/python/project_api.mdx)。

默认 compose 的 event profile 使用 PostgreSQL + Qdrant；declarative profile 使用 PostgreSQL + Neo4j；不是必须同时起所有后端，也不能把它概括成只需一个 pgvector 表。[部署说明](https://github.com/MemMachine/MemMachine/blob/d5a39a8e774e977fb1e6c22efb03555c4e51e05e/DOCKER_COMPOSE_README.md)。

**推断**：若重要目标是跨会话按完整事件回溯、少丢原文且整合 semantic 引用，MemMachine 比另造 episodic 系统更值得对照。但 SupplyAgent 已有 Trace/Artifact，仍需评估双份原文的保留、删除及证据权威问题；其内建 short-term 不能直接保证本项目 Pi tool pairing/Job generation 语义。

## 选择建议与翻转条件

**建议主选 Mem0 OSS，使用完整业务治理适配，而非“因为表少先做轻量记忆”。** 理由是 SupplyAgent 的长期事实、偏好、经验可复用它的 additive extraction、混合检索与可配置组件，且保留现有 Python/SQL 业务权威及 Pi 工具循环。短期 Context Compiler、消息配对、租约、业务事实核验、版本冲突与删除协议必须由产品拥有；换其他框架也不消失。

本建议是适配性推断，尚未用采购业务基准证明 Mem0 最优。框架选择必须经过相同采购任务数据集：客户偏好纠错、多来源相同事实、旧 BOM/过期报价、撤权、工具失败、跨组织同名实体、会话恢复、PII 擦除、索引延迟、写入半成功。分别测量引用正确率、当前有效版本命中、禁止信息泄漏、重复率、上下文 token、模型/embedding 消耗和恢复时间。先达到安全与正确性门槛，再比较延迟和成本。

| 什么时候调整主选 | 下一候选 / 方式 |
|---|---|
| 全文事件与 profile 一体化收益明显，双份 transcript 可治理 | MemMachine 同业务样本对照 |
| 跨模态、图关系和持续调度成为真实必要能力 | MemOS 服务对照，Dream 仍按 beta 隔离 |
| 资源/文档/记忆统一文件层级与渐进检索成为主要瓶颈 | OpenViking 对照，并评估当前许可和服务边界 |
| Mem0 候选隔离、重复 embedding 或删除限制成本过高 | 以测得成本比较上述候选；不通过绕过私有 API 隐藏成本 |

不同时引入四套记忆系统。先用一个可替换 Adapter 验证选定路径，保留 framework/version/source metadata 与重新构建投影的原文证据。上述门槛通过前保持 contract_draft，不把架构方案写成已具备能力。
