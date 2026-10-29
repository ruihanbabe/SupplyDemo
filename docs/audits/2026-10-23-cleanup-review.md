# 全库过期内容清理审阅单

> **文档契约** · 类型：参考 · 读取：批准清理范围前审阅
> 更新：清理候选或依赖证据变化时，由用户授权的维护者更新
> 独占：本次清理候选、证据、建议动作、保留项与批准边界
> 不收录：产品需求正文、凭证、客户正文、尚未执行的删除/迁移成功声明

日期：2026-10-23。状态：**待用户批准，本轮未移除任何已有文件或数据库对象**。

## 1. 结论与建议批准范围

需要清理，但应清掉过期入口、重复设计与已无消费者的数据库对象，不按 Feature 编号或文件日期整段删除。

建议批准 **A01–A03、B01–B10**；数据库 **C01–C03** 单独列明后，可在同一次批准中纳入，但实施必须先备份、克隆验证，再应用到明确的本地业务库。**A04、C04–C06、D01–D02 不在默认执行范围内。** 当前四场景、审批安全边界及后续 Memory/Context/Tool Runtime 目标保留，不把“延期”改成“取消”。

其中，A01–A03 共 4 个文件、680 行、38,564 字节，可移除；B 类是删去过期段落、合并后再删除重复文件，不是整份需求作废。SQL 清理不会重写已应用 migration，不清空 `app` schema。

逐文件的 184 项覆盖记录、大小、建议动作及内容 SHA-256 见 [扫描清单](2026-10-23-cleanup-inventory.json)。批准实施时应先比对这些指纹，避免覆盖扫描后新增的工作。

## 2. 扫描范围与证据边界

- 扫描前有效工作树共 **184 个文件**，合计 1,467,441 字节；含 40 份 Markdown、65 个 Python 文件、34 个 SQL 文件。额外检查 Makefile、配置、前端/Node 入口与锁文件。
- 范围：根文档/命令、23 个产品源码文件、33 个测试/fixture 文件、30 个前端文件、3 个 Pi 文件、21 份 Contract、10 个 Alembic 文件、34 个产品 SQL 文件、业务配置及数据样例。扫描为内容/引用/依赖审计，不是逐行业务正确性评审。
- 排除：依赖安装目录、构建/测试缓存、Git 对象、真实 `.env` 内容及个人 `.claude/settings.local.json`。数据库连接仅由本地配置解析，不收集或输出凭证。
- 当前已有 **209 个暂存删除**，属于此前清理；本次不恢复、不重复计算，也不重置暂存区。另有大量当前开发改动/未跟踪文件，均保留。
- 数据库只读核查了系统目录、对象依赖、权限和旧表行数，没有读取客户正文，也没有执行变更 SQL、启动新服务或调用模型。
- 本轮没有重跑 `make check` / 全量集成；106 个离线通过、13 个集成通过/1 个 F41 失败是上轮证据，不冒充本次执行。

## 3. A 类：建议移除的文件

| 编号 | 精确对象 | 过期证据 | 建议动作/影响 |
|---|---|---|---|
| A01 | `frontend/index.html` | 旧单页请求 `/api/chat/stream`，当前 Python/Next 都无该接口；Next 使用 `src/app/page.tsx`，package scripts 不加载该文件 | 删除整个文件；保留现有 Next 工作台。不能把旧页面中的多 Provider 卡片当成已接入能力 |
| A02 | `CODING_RULES.md` | SSOT 指向不存在的 PROGRESS、DECISIONS、ARCHITECTURE、DEVELOPMENT、docs/features.json 及旧 docs/spec 体系，与当前 AGENTS/spec/HANDOFF 分工冲突 | 删除整个旧治理文件；有用且未重复的原则只合并到现有入口，不重建已删除的文档体系 |
| A03 | `docs/handoffs/2026-10-12-f39-authorization-refactor.md`；`docs/handoffs/2026-10-14-f39-f44-to-f42-session-handoff.md` | 仍以旧提交和 F42 未开工状态提供下一步，独有边界已由 F39/F43/F44/F42 Contract 承接；当前启动入口没有引用它们 | 删除两份过期交接；保留当前 HANDOFF 与各 Contract 的实际验收事实 |
| A04（可选） | `frontend/src/app/api/v1/crm/{actions,customers,interactions,opportunities,proposals}/route.ts` 共 5 个文件 | 只有薄代理；当前页面没有调用，Python 没有对应 CRM HTTP 路由，完整 F32 仍 deferred | 可删除未接通入口以缩小暴露面；这会改变 Next 路由清单，需单独选择。保留 F32 目标、CRM 表、事实 Repository 与测试 |

A01–A03 无当前运行代码消费者。A04 是未实现能力的占位入口，而不是已确认废弃业务，故不默认删除。

## 4. B 类：保留有效内容，清除过期与重复正文

| 编号 | 精确文件/段落 | 发现的问题 | 建议清理后的唯一落点 |
|---|---|---|---|
| B01 | `todo.md` 的“上一轮架构对照答复存档”、旧全链路时序/确认目录 | 230 行混有已实现前的角色/模型/工具状态，与 F45–F50、当前数据基线重复 | 改成简短的未决设计队列：Context、Mem0、Tool Runtime、恢复、协作、Eval；删除长对话和已完成项，引用对应 Contract |
| B02 | `schema-plan.md` 全文整合 | “缺 BOM/报价”“四个 R0 工具”“model_call 新表”及自有 memory_revision 候选已被后续实现/选型部分替代；S01–S12 是未批准候选，却容易被误读为必须补表 | 将仍有效的身份/版本/时序/失效问题合入 `data-contract.md` 的未决项，物理追加以 F45–F50 为准；修复引用后删除重复 `schema-plan.md`，不按候选表名建表 |
| B03 | `data-contract.md` 第一部分、旧“后续讨论入口” | 通用设计与当前第二部分并列，仍要求先做已完成的业务故事；部分 current/待实现表述是历史快照 | 保留一个“当前已实现交接基线”正文；通用 Task/Context 版本链/等待恢复作为明确未决项。避免重复表或误称所有未来 schema 已冻结 |
| B04 | `docs/design/model-gateway-worker-contract.md` 第 3–7 节与第 8 节 | 同一文件同时维护实施前缺口、建议和实施后接口；旧表仍说没有探针/报告 | 保留范围、职责、当前接口/报告/命令/验收及未解决限制；删除已经被第 8 节取代的计划流水，保留令牌不绑定活动租约这一真实限制 |
| B05 | `business-scenarios.md` §6“本轮资源核查快照”、§8“下一步产物”及旧状态段落 | 本机凭证“有无”与 hw_part 快照不应长期充当业务需求；下一步仍指向已完成故事/首个链路 | 四业务场景、节点责任与输入输出需求继续保留；当前实现状态只引用 features/Contract，不重抄资源及凭证快照 |
| B06 | `contracts/F32,F33,F34,F35,F37,F38,F41,F42,F45,F46,F49.md` | F34/F49 仍有“仅 Responses/无 Chat”；F45 限 OpenAI/旧单料号状态；F46 仍说下一步补 BOM；F35 同时保留未做进程演练与新演练记录；F32 事实只供 F36 的声明已被 F45 扩展；F37/F41 仍指向已删除的“旧审批资产” | 保留各 Feature 边界和必要验收证据，按 F50/当前链路改正有效声明；F33 等明确目标与当前实现分开。历史测试数量本身不是错误，可压缩为日期与证据指针，不伪造新验收 |
| B07 | `AGENTS.md`、`spec.md`、`HANDOFF.md`、`Makefile` 及被牵连指针 | AGENTS 引用不存在的 spec §0.5/§11，将 G1–G8 指到 §0.4（实际 §0.3）；列出已不存在的 build-parts/fetch-hardware/prune-hardware；Make 注释指向 DECISIONS D14；spec 使用 sessions/agent_run_states/job_queue/event_log 等并不存在的当前表名 | 修正现有入口和章节；移除失效命令，不恢复旧硬件模块。spec 分开当前能力与未来目标；Web 单入口不等于自动取消其他三业务场景或审批安全边界 |
| B08 | `.env.example` 的旧 LLM、供应商、ERP、数据库/Redis变量及说明 | 有 19 个模板变量无当前源码/测试/命令消费者；“SUPPLYAGENT_LLM_ENABLED 是全局真实调用开关”“每 Worker 由 YAML 配模型”“填供应商 Key 即可真实调用”等已不成立 | 删除无消费者模板项及误导说明，未来供应商/ERP 接口只留需求指针；保留实际 Gateway/Worker/Auth/业务开关。真实 `.env` 和已有密钥一律不修改 |
| B09 | `requirement.txt` | 自研 Runtime、LLM 待选、YAML 配置/asyncpg 主连接等描述过期；含已删除决策文件、机器环境与不再适用的框架排除理由；没有准确反映 Pi 和 psycopg 产品路径 | 改成短的当前依赖用途/维护入口；机器包版本与传递关系仍由锁文件负责。只改说明，不在此项卸载依赖 |
| B10 | `data/knowledge/demo/hackrf-one-product-context.md` 的本地来源声明及相应文档引用 | 声称本仓库仍保留 `data/hardware/raw/.../MANIFEST.json`，并可由现存源码重建 hw_part；对应文件/命令已被删除 | 保留有来源的官方摘要及适用限制，移除不存在的本地来源声明；样例内容变化按知识导入/版本契约验证，不删除官方摘要、manifest 或全部演示语料 |

B06 不是删除 11 份 Contract。特别是 F32/F33/F37/F41/F43 的“未做/延期”不等于“无意义”。F30–F50 的 Feature 身份不重排，功能删除须有另外的范围决定。

### B08 的 19 个无消费者模板变量

仅列变量名，不涉及真实值：

- `SUPPLYAGENT_DATABASE_URL`
- `SUPPLYAGENT_REDIS_HOST`、`SUPPLYAGENT_REDIS_URL`（Redis 容器的 PORT 仍有 Compose 消费者）
- `SUPPLYAGENT_LLM_ENABLED`、`SUPPLYAGENT_LLM_ZHIPU_BASE_URL`、`SUPPLYAGENT_LLM_API_KEY`、`SUPPLYAGENT_LLM_BASE_URL`、`SUPPLYAGENT_LLM_TIMEOUT_SECONDS`
- `SUPPLYAGENT_SUPPLIER_MOUSER_API_KEY`、`SUPPLYAGENT_SUPPLIER_ELEMENT14_API_KEY`、`SUPPLYAGENT_SUPPLIER_ELEMENT14_STORE`
- `SUPPLYAGENT_SUPPLIER_DIGIKEY_CLIENT_ID`、`SUPPLYAGENT_SUPPLIER_DIGIKEY_CLIENT_SECRET`、`SUPPLYAGENT_SUPPLIER_DIGIKEY_BASE_URL`
- `SUPPLYAGENT_SUPPLIER_REGION`、`SUPPLYAGENT_SUPPLIER_CURRENCY`
- `SUPPLYAGENT_ERP_BASE_URL`、`SUPPLYAGENT_ERP_API_KEY`、`SUPPLYAGENT_ERP_API_SECRET`

**保留** `SUPPLYAGENT_LLM_ZHIPU_API_KEY`：当前仍是实际 fallback。`SUPPLYAGENT_MODEL_SECRET_<REF>` 属动态凭证槽，不按“未出现完整变量名”判死。Worker 的可选 lease/heartbeat/retry 参数亦仍有消费者。

## 5. C 类：数据库对象清理候选与明确保留项

### 5.1 实际结构快照

本地实际有 `app` 29 张表，均启用并强制 RLS；`public` 有 12 张旧业务表及 `alembic_version`，Alembic 版本为 0008。当前没有发现产品逐条 migration 校验和账本，不能仅凭对象存在声称某文件已按现有字节完整应用。

`app.action_proposal`、`app.intelligence_request`、`app.catalog_product`、`app.model_exchange` 均不存在，符合此前业务库未应用 0023 草稿及 0024–0028 的边界。未应用的新实现不是过期 SQL。

| 编号 | 精确对象 | 当前证据与建议 |
|---|---|---|
| C01 | `public.artifact_dep`、`artifact`、`metering`、`tool_record`、`session_event`、`datasheet_page`、`datasheet_version`、`kb_status`、`kb_entry`、`env_write`、`env_snapshot`，以及它们专用的 `public.env_append_only()` | 11 表本次精确 COUNT 均为 0；当前产品源码/测试无业务读取者，已知 FK 在旧表内部。可在新增 legacy Alembic 退役 migration 中按依赖序移除，函数最后删；保留原 0001–0008 和版本记录。执行前再次验证为空、备份结构/权限、克隆验证且使用 RESTRICT，发现新依赖即停止 |
| C02 | `app.propose_long_term_memory(jsonb,uuid,uuid,uuid)` 旧四参数重载 | 本地仍存在，当前迁移/调用只定义或使用五参数版本；仍授予 supplyagent_api 执行，目录没有找到静态 dependents。建议在新的产品清理 migration 中精确删除旧签名，保留 `app.propose_long_term_memory(jsonb,jsonb,uuid,uuid,uuid)` 和现有表/生命周期。它是真实结构漂移，不是删同名函数全部重载 |
| C03 | `app.begin_runtime_run(text)` | 旧入口仅创建无业务 scope/Trace/Job 的 Mission/Run；当前产品、测试和已检查的 app 函数正文均无调用，目录未发现 dependents。建议精确移除旧入口，保留 begin_supply/opportunity/intelligence 等实际受控入口；若执行前发现外部消费者则停止 |
| C04（不默认删） | `public.hw_part` | **117 行**，不是空表。当前产品无直接读取，但用户曾明确依赖其 BOM 来源；原始文件和重建程序已删除。默认保留隔离，若要移除，必须另行确认导出数据/来源快照及恢复验证，不能声称可随时用现存 make 命令重建 |
| C05（保留） | `app.crm_proposal` 及 create/review/reorder CRM SQL；`0023_f41_proposals.sql` 和 `tests/test_product_api_f41_integration.py` | 旧 CRM SQL 仍被 `supabase/tests/f32_crm.sql` 使用；F41 是暂停而非取消，0023 只在草稿测试克隆应用。默认均保留，F41 后续决定统一/迁移；不能为了全量变绿删除失败测试或伪装审批已完成 |
| C06（保留） | `public.alembic_version`、`public` schema、`public.pgcrypto` 扩展、旧角色与全部已应用 migration | `public.digest` 被 app Trace/知识/预算调用；删 public schema 会破坏当前系统。旧 supplyagent_app/agent_ro/procurement 为集群级角色，可能被其他数据库依赖，不在本库表清理中直接 DROP ROLE |

C01 的 11 张表可以清理，不代表能删除整个 `alembic/`。C02/C03 可以作为新增产品清理 migration；不回写旧 migration 来假装旧对象一直不存在，也不重排 0023–0028。

已知静态/catalog 无依赖只说明本次覆盖的仓库与本地数据库未发现依赖，不证明外部脚本或其他部署一定无消费者。SQL 执行仍须明确目标库；本审阅单不授权远端或生产变更。

### 5.2 不应误删的当前 SQL 与代码

- `supabase/migrations/0001–0022` 的实际历史（当前没有 0013 文件，不填号、不重排），以及 `0024–0028` 的已验证新实现；函数被后续 migration 替换不等于前序文件可删。
- `src/product_api/memory.py`、`app.long_term_memory`、0022、当前记忆/知识触发器和测试。它们仍被 F36 Mission 使用；F48 明确要求切换 Mem0 前保留历史兼容。
- F36 `mission_runtime.py` 与 F45 `intelligence.py` 都有调用者和集成测试；并存不直接判为重复死代码，不在清理中重构两条业务路径。
- F49 Responses 实现/测试、F50 GLM 兼容实现/测试及 Pi 固定版本；当前已明确保留两条协议能力。
- F43 在途一致性缺口、F38 Eval、Tool Binding/Runtime、权限/撤销/恢复边界仍有业务意义，不能为减文档删除需求。

## 6. D 类：可选减规模，不默认实施

| 编号 | 范围 | 证据与边界 |
|---|---|---|
| D01 | `asyncpg`、`PyYAML`、`redis`、`pydantic-settings`、`mypy`、`pytest-asyncio` | Python AST/命令/配置扫描未发现当前直接使用；可另做依赖瘦身。必须核对传递依赖/pytest 插件、重建锁并在干净环境验证，不能只删 lock 行。SQLAlchemy/alembic 仍用于保留的历史维护，psycopg/pypdf/Node Pi 等有实际消费者 |
| D02 | Compose Redis 服务及 smoke 对应部分 | 当前 Python 产品未使用 redis 客户端，SQL 才是队列；但 Redis 已运行，spec 保留协调/缓存目标，停止它属于运行拓扑改变。可单独批准从默认启动中去除，当前建议保留至工具 Runtime 设计确认需求 |

另发现 legacy Alembic 维护入口使用的当前 URL 默认被 SQLAlchemy 解析为 psycopg2 驱动，而本环境未安装 psycopg2。这里只做静态 URL/模块检查，没有执行迁移。若批准 C01，先将维护连接明确到已安装的 psycopg 驱动并在克隆库验证，不新增驱动依赖、不碰用户凭证值。

## 7. 批准后的执行与验收顺序

此清理涉及超过 8 个文件及数据库对象，应按独立单元顺序执行：

1. **仓库清理单元**：A01–A03、B01–B10，合并有效内容后删重复文件；一次性修复所有消费者、链接和不存在的命令/章节引用。需求目标、当前未决项和实际验收证据不丢失。A04 只有被明确选中才加入。
2. **数据库退役单元**（仅批准 C01–C03 时）：检查扫描后无变化；对目标对象备份结构/权限，旧数据仍为零才推进。C01 归 legacy Alembic 新增 migration；C02/C03 归产品新增 migration，禁止交叉管理。先在自动删除克隆库验证，再对用户明确授权的本地业务库执行；不因此自动应用 0023–0028。
3. **收口**：更新当前 Contract/HANDOFF、features 的证据指针，保留用户验收状态；将本审阅单标记成已执行范围/未执行范围，不再把它当另一份需求正文。后续从精简后的 data-contract 与未决队列开始 Memory/Context/Tool Runtime 设计。

验证要求：

- 全库引用、Markdown/文本路径、Makefile 目标、Python/Node 导入与前端边界扫描；所有保留入口可定位。
- `make check`；若删除 A04，运行前端边界/lint/build，既有 Next 安全阻断要如实记录，不以跳过替代通过。
- 克隆库验证 C01 的空表退役及 app 表/函数未受影响；C02 精确旧重载不存在、新重载可用；C03 旧入口不存在、当前创建/Worker/Trace/记忆链路正常。
- 运行 Gateway ready、Runtime、F36/F45/F47/F50 对应克隆库验证及全量集成。已知 F41 失败不以删测试/加 skip/降断言消除。
- 检查恢复方法，数据库变更失败事务回滚；成功后需要恢复时使用已验证备份，不靠重建缺失硬件源猜测数据。保留 `.env`、个人配置及已存在暂存/未暂存工作。

本次审阅只新增本文件和扫描清单两个审计产物；既有文件、schema、数据、依赖及运行服务均未清理。
