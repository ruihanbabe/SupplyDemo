# SupplyAgent v0.6 交接

## 2026-10-29：AgentScope 迁移计划与远端基线

用户明确项目用于求职展示，后续方向为先验证 AgentScope SDK 与 SupplyAgent 业务服务，再依据证据重构 Agent SDK。计划见 [独立迁移 TODO](TODO-agentscope-migration.md)，本轮授权仅记录计划、核查提交范围并保存远端基线，不开始 SDK 接入或继续 F37/时钟排查。

当前工作树已有 F53 A 的 SDK / Service 包归类与 product_api 兼容入口，作为迁移前现状保存；不代表 AgentScope 已接入或反向依赖已消除。本次检查为 make check 281 passed/91 deselected，make web-check 全部通过；未运行数据库集成或真实调用。提交范围、验证与保留限制见 [基线记录](docs/audits/2026-10-29-pre-agentscope-baseline.md)。以下开发续接均为此前状态，不能覆盖本轮迁移计划。

## 最新指示与续接（2026-10-28，覆盖下方时钟排查计划）

用户要求将时钟错乱记录到Feature开发总结，**跳过该问题，继续下一部分开发**。已写入`docs/audits/2026-10-28-implementation-summary.md`与todo，不再继续时钟探针、系统校时调整或重复全量碰绿。继续F37当前切片验证、Contract/Feature状态与本轮摘要收尾。

续进结果：已修正F37测试夹具的无关供应调查Job残留，显式取消种子Job后未审批断言通过，消息路径禁调用模型断言保留。f37-check=2passed/lint通过；最新f37-integration-test=10passed/1failed（lost_response首次领取idle），不再为时钟问题重跑。make check=232passed/91deselected/1既有提示，git diff --check和Feature ID唯一性通过。F37仍in_progress，其他当前切片awaiting_acceptance；完整实现/测试/用户联调前置摘要已写并同步Contract/todo。无运行中的本轮检查；下一步不再排查时钟或扩展真实渠道范围。

最小remember→claim压力复现第15轮：due_at=05:46:41.857932UTC，随后数据库clock_timestamp=05:46:41.451870UTC，pending/generation0未领取；同秒Lima日志校时477ms。900次独立连接捕获4次回跳；停NTP700次仍1次、换acpi_pm900次仍3次，未修复。已确认systemd-timesyncd=active、clocksource=hpet，恢复原配置。原全量86passed/3failed保留；窄回归unknown+整个memory_governance共7passed，不冒充全量全绿。首次三个失败未各自抓到时钟诊断，因果关联有此证据边界。

日志在`.runtime/verification/`忽略目录，完整说明/路径/官方源码参考在实现摘要。临时Memory测试诊断已清除；没有修改业务领取/到期/租约逻辑、加skip或放宽断言。下方“继续排查共同根因”与环境改动建议已经失效。

## 压缩续接检查点（2026-10-28，优先于下方过程记录）

### 授权与范围

- 用户已授权按F38入口评测→F33只读可视化→恢复增强→F41/F37顺序开发，并多次要求不要停下/不重复确认；最新指令“压缩当前窗口上下文后再继续开发”。压缩后继续F41/F37收尾，无需重新授权。
- 当前唯一in_progress为F37；F38/F33/F35/F41当前切片awaiting_acceptance。它们不是全部长期目标完成。用户自行做浏览器/真实渠道端到端联调；我们执行实现所需本地自动化回归。真实模型/渠道、业务库迁移、部署仍未执行/未授权。
- 巨大既有staged/unstaged/untracked树，HEAD=b16c926，分支spec-v0.2-harness-reset。保留全部用户改动/暂存，不碰.claude/settings.local.json/真实.env，不提交、不清理旧工作。

### 已实现与落点（不要从零重做）

- F38：evaluation.py + config/product/eval-cases.json + evaluation_result.py。固定pytest案例runner、版本/源码指纹、跳过/空执行非通过；原用户范围GET /v1/intelligence/runs/{run_id}/evaluation快照，硬约束与绑定caseHash/snapshotHash的人工量表，费用unknown/已结算分开。EVALUATION.md/Contract已有边界，真实语义评审并未假造完成。
- F33：原用户范围SSE GET /v1/intelligence/runs/{run_id}/events、每批scope重验、Last-Event-ID、终态/撤权关闭；Next BFF纯代理、页面最近500条事件与本Run用量。8专项/3双协议克隆/224离线阶段通过；真实浏览器/生产build未验收，既有SecurityFatalError未解决。
- F35 R1：recovery.py固定失败分类/checkpoint hooks；HarnessTurnError.reason透传；业务checkpoint在有效Job事务下写Artifact/Event；新Worker ModelCapability绑定job/owner/generation，Gateway新请求重验。0042只新增受限锁函数。19专项/5克隆/230离线阶段通过；自动角色复用/通用人工等待/流已发出撤销仍后续。
- F41：actions.py、app.py人工提案/审阅/读取路由、Worker action_execution分支、0043。人工customer_update；只接受本人manual_action_evidence（F40 SQL真hash），禁止自批/重复审阅；可选idempotencyKey，同内容复用/冲突409；审批同事务入Job/独立Trace/账本；有效租约下重验双方权限/scope/hash/到期/版本/opt-out/服务revision/证据，本地SQL更新+成功账本原子，Job重投不重复更新。20专项/8克隆通过。政策默认缺省不可执行，草稿revision=0不随未来启用复活；政策变更必须递增revision，时长配置max_approval_seconds。
- F37：channels.py、0044、MessageProposalInput与POST /v1/message-proposals、解密GET审阅；复用F41审批/Job，不直发。Fernet加密正文/目标、HMAC语义指纹保证随机密文幂等；SQLite独立接收方持久logical_action_id去重/lookup；先sending意图，未知只查不盲发；accepted≠delivered；fixture HMAC时间窗回执、inbox加密/事件ID冲突拒绝、终态不倒退；取消后迟到回执记录实际事实而不将cancelled Job改completed。
- F37仅fixture实现；WhatsApp/飞书真实协议/账号/会话/HTTP webhook入口未实现，不能称已接通。Pi仍只读，无R1工具或密钥。真实采购/完整审批UI/全局配额等仍后续。
- 可选requirements.channels.lock.txt=cryptography46.0.7/cffi2.1.1/pycparser3.12，已清洁venv验证加解密/错钥，再安装当前.venv；pip check通过。50系列需要本机源码构建，试装已停止，未采用。主锁未改，.env.example仅增加off/空密钥；测试临时注入密钥。f37-setup是唯一安装入口。
- 模块运行说明ACTIONS_RUNTIME.md；BL10/DC22已更新，Gateway运行说明/机制索引也同步；所有0042–0044及0023仅自动删除克隆应用，业务库未迁移。

### 当前精确验证与未解决失败

- 最新make check：232 passed、91 deselected、1既有Starlette提示；git diff --check及Feature ID唯一性通过。新F37阶段10克隆通过，取消迟到专项1通过；最新全量覆盖11个F37变体及8个F41变体。
- 最新make test-integration已结束：**3 failed,86 passed,234 deselected,2 warnings，364.64秒**。不能报全量绿色。完整输出保存.runtime/verification/full-integration-2026-10-28.txt（私有/忽略），functions store integration_final也曾保留。
- 失败1：tests/test_product_api_channels_integration.py::test_encrypted_send_gates_receipts_and_unknown_reconciliation[unknown]，line141断言第三次Worker结果failed，实际idle。前两次retry_scheduled且发送仅1次。需要查该时刻Job状态/attempts/visible_at/锁和claim；不要直接改成轮询碰绿或放宽断言。
- 失败2：tests/test_product_api_memory_governance_integration.py::test_entity_qualifiers_and_multisource_relations[decision episode]，preference helper line33期待review_pending，实际{'status':'idle'}。
- 失败3：同文件test_legacy_export_revalidate_reconcile_switch_and_safe_rollback[False]，line155期待review_pending，实际idle。
- 共同线索是“立即领取返回idle”，**尚未确定共同根因**。SQL0019 claim_runtime_job与0035 memory_claim均用clock_timestamp + FOR UPDATE SKIP LOCKED；memory_claim没有新覆盖。MemoryStore.remember/claim需定点读。上次到这里仅核查SQL，未改变任何等待/次数/断言。
- F37此前cancel_after_commit失败是真实原因映射：tenant.py把42501包装TenantAccessError；ChannelExecutor.command现在从__cause__识别固定lease_lost→ChannelError→HarnessTurnError，Worker读取持久cancelled。专项通过；全量该变体也通过。
- F37最初装配失败是tool_database默认每迁移执行2遍而0023非幂等；已新增once_migrations参数，仅0023单次，0031/0043/0044仍重复验证，不改历史0023。

### 下一步（继续执行，不重启规划）

1. 读私有失败日志的对应断言附近；给三个idle边界加安全状态诊断（Job/work_item行、due/visible与服务器clock、锁），在窄用例或可控并发下确定原因。无真实模型/业务库修改。
2. 修正被证明的问题，保留失败/权限/未知结果断言；若只是既有无锁任务暂不可领，必须用证据确认再定义可观察完成边界，不能猜测/盲重试。
3. 跑窄回归，再make f37-integration-test / f41-integration-test及必要全量门禁；全部必要检查过后才将F37当前切片awaiting_acceptance。
4. 汇总已实现/验证/保留限制到docs/audits的实现摘要，同步HANDOFF/Contract/Feature/todo；当前该最终摘要尚未写，F37 Contract证据也待收尾。不要把真实渠道/生产或全部长期Feature写成完成。

### 命令与现场

- Make：eval-check/eval-run/eval-integration/eval-score，f33-check/f33-integration-test，recovery-check/recovery-integration-test，f41-check/f41-integration-test，f37-setup/f37-check/f37-integration-test，make check / test-integration。
- 所有当前工具检查都已结束，**没有运行中的本轮检查**；不等待旧session。未创建subagent、未发送外部消息、未提交/暂存。
- 目前数据库服务正常，问题不是已知Colima启动失败。不要重迁源库。tests/test_product_api_tool_runtime_integration.py的tool_database克隆会重复应用0030/0042与传入migrations，并单次应用once_migrations；该通用helper也被F48使用。

## 2026-10-28 最新：F41/F37当前推进

用户多次明确继续、不停在授权确认；最新要求继续F41/F37。已依次实现F38 E0/E1当前入口评测、F33私有SSE/续接/时间线/本Run用量、F35 R1失败分流/checkpoint hooks/Job绑定模型能力；均为当前切片本地交付，完整长期目标保留。当前唯一活跃单元F37本地加密fixture发送/回执/对账；F41人工customer_update已通过20专项/8克隆及230离线阶段回归，后续同键/期限与服务policy边界补充以最终记录为准。

F37已新增0044、channels.py、消息提案/解密审阅API，复用F41审批与F35 Worker；独立SQLite远端、加密、先意图后发送、未知只lookup、签名回执幂等与accepted/delivered区分。默认真实渠道拒绝，WhatsApp/飞书真实协议/会话接线仍未实现，不可把fixture叫作实接。

可选requirements.channels.lock.txt固定cryptography46.0.7/cffi2.1.1/pycparser3.12；清洁环境加解密/错钥测试通过，随后已安装当前.venv并pip check通过。50系列本机需要源码构建，试装已停止，未采用；主锁/真实.env未改。新增服务端配置仅.env.example默认off/空密钥，测试均临时密钥。

最新离线make check为232 passed/91 deselected；全量make test-integration正在执行，已见1失败、尚待完整原因；不得声称全量绿色。F37阶段10克隆通过；取消后迟到结果曾因TenantAccessError包装隐藏lease_lost失败，已按cause修复并专项1通过，新增最后断言仍由全量回归验证。0042/0043/0044仅克隆，未迁移业务库。ACTIONS_RUNTIME.md和BL10/DC22维护当前边界；最终源码交付摘要待全量收尾后补。

下一步先读取全量集成完整失败，按原因修复并执行窄回归，再完成F37与全量门禁/文档收尾。真实Provider/渠道发送、业务库迁移、部署仍需独立授权；不触碰既有暂存或.claude/settings.local.json。以下E0段是历史检查点，不能覆盖本节。

## 2026-10-28 早期检查点：顺序开发授权与F38 E0

用户授权按F38当前入口评测→F33只读可视化→恢复增强→F41/F37执行闭环顺序推进，全部完成后汇总。一次一个单元；当前仅F38 E0活跃，其他单元尚未开始，不要求用户重复授权。真实Provider/渠道发送、源库迁移、生产部署仍不在自动执行范围。

E0新增evaluation.py、eval-cases.json及报告门禁测试；Make入口eval-check/eval-run/eval-integration。7门禁自检、8离线案例通过；集成2案例/3参数化测试通过，无跳过，两报告sourceUnchanged=true。make check为217 passed/72 deselected/1既有警告。新runner只作contract_regression，语义not_measured、费用NULL，不声称完整Eval。实现入口src/product_api/EVALUATION.md；F38 Contract已冻结E0边界，整体Feature保持in_progress。所有本轮命令均已结束，无后台检查。

下一单元仍在F38：补真实调查结果的版本化输入/预期与逐Run证据归因、人工语义量表记录及费用/延迟统计，不能用现有pytest包装代替完整入口Eval。完成当前F38范围后再推进F33，随后恢复、F41/F37；不要跳过或宣称本次全部实现。

F33/恢复/F41/F37尚未实现本次新增范围；不得把此次授权或E0通过写成全部完成。保留全部既有工作树与暂存，未碰.claude/settings.local.json、真实.env、依赖/锁文件或业务库。

> 更新：2026-10-27。新会话依次阅读本文件、`AGENTS.md`、当前 Feature Contract 与 `features.json`；只按 Contract 引用定点阅读 `spec.md`。

## 当前产品边界

SupplyAgent 是 AI Native CRM 与供应链处置系统。Python API/Worker/Harness、PostgreSQL/RLS 负责组织、业务状态、工具授权、模型 Gateway、持久运行、Trace 和副作用；Node Pi sidecar 只执行短生命周期、无状态的 Agent turn。

模型不得获得凭证、SQL、文件系统、持久 checkpoint 或直接副作用。所有外部调用、付费模型、渠道发送和批量检索仍需 G5 授权；R2 副作用仍需 F41 的 Proposal/审批/重校验边界。

## 当前状态

- F51 P1–P4 已实现并通过克隆验证，等待用户验收；Python 独占 Runtime/Scheduler，保留 v1 显式回归。0030/0031 未迁移业务库；技术选择、验收矩阵和精确证据见 contracts/F51.md。最新 F52 C1–C3 本地实现待验收，默认仍关闭；F48 M1 开发中，以文末续接和各 Contract 为准。

- F34/F35 运行补充已实现待验收：Gateway 存活/就绪检查、Worker 单次入口、安全报告与停止语义已完成；证据及保留限制统一见 [运行说明](docs/design/model-gateway-worker-contract.md)。当前可供 Memory/Context 设计使用的交接基线见 [data-contract.md](data-contract.md)。未新增业务 schema。

- F50 已实现待验收：当前模型选型 GLM-4.7-Flash，经 Pi 的 OpenAI 兼容 Chat Completions 接入。真实单角色合成冒烟已通过（2 请求/1 工具），GLM/Responses 五角色克隆验证均通过；上线/迁移清单及证据见 contracts/F50.md，0028 未应用业务库。

- F49 已实现并通过本地验证，等待验收：F49 阶段仅支持 OpenAI Responses，复用 Pi 原生 Adapter；Gateway 和私有模型交换已在克隆库验证，业务库未应用 0024–0027。配置、验收与真实调用前置条件见 contracts/F49.md；GLM 后续由 F50 承接，审批、渠道或 Mem0 SDK 继续暂缓。

- F47 BOM 版本关联与报价时效 Specialist 已实现并通过克隆库验证，等待用户验收；0026 未应用业务库，详见 contracts/F47.md。F48 已进入合成 M1 闭环开发，业务库尚未迁移。用户明确审批和渠道执行暂时不做。

- F46 首个数据闭环已实现并通过克隆库验证，等待验收。新增字段/模式/种子与后续缺口统一见 contracts/F46.md；0025 未应用业务库。产品/BOM 版本关系及报价时效由 F47 承接；漏斗聚合仍待后续单元，审批/写入/发送继续暂缓。

- F30 的遗留清理已完成并等待用户验收；F35 和 F36 已获本地确定性 Replay 与 PostgreSQL/RLS 证据验收。不代表真实 Provider、生产部署或副作用已验证。
- F31、F34、F39、F40、F42、F44 等仍按 `features.json` 和各自 Contract 的状态处理，不能因已有代码或迁移自动扩大完成范围。
- F45 商机情报问答代码与本地 Replay 证据已交付，等待验收；0024 尚未应用业务库，web-check 存在依赖安全阻断及一次未复现的领取异常，详见 contracts/F45.md。续验已修复并测试当前成员角色查询错误，记录见同一 Contract。F41 未完成的草稿保留；其余销售运营、客户沟通和主管巡检仍有后续工作；F47 后续 Responses 接入由 F49 承接，不实施审批/渠道。

## 2026-10-21 清理决定

- 已按用户确认移除早期单租户 Harness 的实现、测试、配置、Contract、样例、Skills 与叙事；不得恢复或从它们复用代码。
- `alembic/` 仅保留旧数据库 migration 历史。当前产品 schema 只通过 `supabase/migrations/` 和各 Feature 的 `*-migrate` 目标管理。
- 当前 F36 演示知识在 `data/knowledge/demo/`，不依赖已删除的旧硬件原始层。
- 不触碰 `.claude/settings.local.json`；它是用户本地配置，保持未暂存。

## 可执行验证

- 离线门禁：`make check`。
- 各 Feature 的 scope 验证以 Contract 和 Makefile 中对应的 `*-check` / `*-test` / `*-integration-test` 为准。
- 清理后优先验证 `features.json` JSON、F30+ 引用边界、保留源码的导入边界，再运行 `make check`。

下一步：F51 P1–P4、F52 C1–C3 已本地实现待验收；当前继续 F48 M1，精确范围和证据见文末。新增表只在克隆验证，不能当作业务库现有能力。真实持续业务联调仍需按 F49/F50 清单确认迁移及组织/Worker/模型配置，前端仍为 Replay。

2026-10-23 运行补充验证（清理前）：`make check` 106 passed；Gateway 专项 11、克隆库 1；Runtime 专项 13、克隆库 1；GLM 兼容专项 2、五角色兼容回归 2。全量集成 13 passed、1 failed，仍为既有 F41 审批路由 404/403 差异；未扩展审批范围。F45 偶发领取失败原因仍未确认，本轮全量通过该场景，不声称已修复。该运行补充未执行真实模型调用、业务库迁移或系统调度器安装，0024–0028 仍未应用业务库。后续已批准清理迁移见下节。

## 2026-10-23 已批准清理与续接入口

- 已执行审阅单 A01–A03、B01–B10、C01–C03。移除过期治理/交接/静态占位页及重复 schema 计划；修正 Contract、指针、配置模板和知识来源声明。未执行可选路由/硬件删除或依赖卸载。
- 项目业务规则集中到 [business-logic.md](business-logic.md) BL01–BL11；跨模块身份、数据时序、状态和持久契约集中到 [data-contract.md](data-contract.md) DC01–DC14。两文均标明影响模块与尚未设计事项；旧 business-scenarios 仅保留兼容入口。
- 已备份并恢复验证本地数据库，然后应用 legacy 0009 和产品 0029：移除 11 张空旧表、专用触发器函数及两个精确旧入口。29 张 app 表及其数据、117 行 hw_part、有效记忆函数、pgcrypto 和角色保留。业务迁移 0023–0028 未应用。
- 恢复备份位于 `.runtime/cleanup-2026-10-23/database-before.dump`（私有、未跟踪）；另有变更前 184 文件归档。精确执行范围与验证结果见 [清理审阅单](docs/audits/2026-10-23-cleanup-review.md#执行结果2026-10-23) 和 F30。
- 本轮离线 106 passed；迁移保护 1、Worker 克隆 1、Gateway 就绪克隆 1、GLM/Responses 克隆 2；前端 boundary/lint 通过。全量集成最终结果见 F30。
- 未调用真实模型、未安装调度器、未改真实 .env/依赖/锁文件、未提交代码。保留用户原有暂存及本地配置。

压缩续接：清理实施完成、等待验收；工具设计见 F51，已确认 Memory/Context 设计见 F48/F52 与 DC18–DC21。详细恢复/协作冲突/Eval 的剩余范围另行分阶段处理，不恢复旧实现，不把目标文档直接当已建表。

## 2026-10-27 工具机制设计续接

- 用户要求将提供的工具机制方案整理为开发 Contract 并下沉，已登记 [F51](contracts/F51.md)，状态 contract_draft；没有激活实施 Feature。
- 架构入口 spec §2.7；业务规则 BL12；身份/调用台账/共享容量 DC15–DC17。开发时只按需要读取 [Python 工具设计](src/product_api/TOOL_RUNTIME.md) 与 [Pi 批次协议](pi_sidecar/TOOL_PROTOCOL.md)，不从交接文档复制接口。
- 当前仍为 Registry 执行工具、Pi 0.73.1 + JSONL v1 串行、F36 Artifact/F45 Observation 分别保存。目标是 Python 独占调度，按 P1 Registry/Binding → P2 Runtime → P3 Scheduler → P4 Pi 桥接逐个验收。
- 方案特别约束：完整 assistant 工具消息封口批次；Pi 本地参数拒绝也必须在下一轮模型请求前归一为 Python 反馈；共享资源失租隔离不因 TTL 自动释放。当前仅源码核对，尚无新机制运行证据。
- 下一步是确认 F51 后实施 P1，保持业务及串行语义；F41 审批/渠道、Mem0/Context 和详细 Provider 即时撤销不在本单元。新增两项持久记录只写目标，不建表、不应用迁移；不改已有暂存及用户配置。
- 本轮文档与离线验证通过，精确结果及未运行项统一见 F51 的“本轮记录”；不代表新工具机制已实现。

## 2026-10-27 Memory / Context 方案确认与开发文档

- 用户确认完整方案并要求补齐开发 Contract 与文档；已更新 [F48](contracts/F48.md)、新增 [F52](contracts/F52.md)，features.json 均 not_started；本轮未激活代码实施。
- 最新架构入口 spec §2.6；长期策略 BL09，短期/装配策略 BL13；来源/状态/版本/加工/删除/快照 DC18–DC21。先前“Mem0 正文、SQL 仅治理”的方向已显式修订为 SQL 确认版本 + Mem0 可重建检索投影。
- 开发顺序固定 F51→F52→F48，一次一个实施单元；F52 的空 MemoryPort 可独立验收，F48 无反向依赖。下一代码单元仍是 F51 P1；本轮只完成开发入口，不据此调用模型或迁移。
- 接口就近查 [Memory](src/product_api/MEMORY_RUNTIME.md)、[Context](src/product_api/CONTEXT_RUNTIME.md)；消息准备/分块查 [Pi Context 协议](pi_sidecar/CONTEXT_PROTOCOL.md)。[原方案入口](docs/design/memory-context-proposal.md) 已改为导航，框架事实保留固定源码研究。
- 真实部署前仍须依赖制品、后端/模型/embedding 配置、费用授权、分类保留与完整擦除证据；不是当前已具备能力。旧 F36 记忆及所有用户原有改动/暂存保持。
- 本轮验证结果和未运行项统一见 F52“本轮文档证据”。

## 2026-10-27 续接：摘要策略调整与工具 P1

- 用户要求先调整 Memory 再继续工具开发。BL09/F48/DC19–DC20/Memory 模块已改为通用过程摘要默认、稳定命题按需；过程摘要自动成为 generated_reference，稳定记忆才确认；不按角色/场景定制写入分类。Memory 未开始实现。
- 当前唯一实施单元是 F51 P1 Registry/Binding；保持串行、领域权限与原产物。P2/P3/P4、F52/F48 尚未实施，实际验证结果见 F51。

### 工具 P1 交付

- 七工具声明/严格 Schema/描述/别名已集中 tool_registry.py；任务 Binding 和 F45 v2 启动快照已接通，Pi 继续串行。P1 本地已验证待验收，F51 整体仍 in_progress，后续 Runtime/Scheduler/批次不算完成。
- 全局阅读入口新增 [项目机制索引](docs/design/project-mechanisms.md)，区分当前实际链路、权限/证据所有权及剩余缺口；对应证据统一见 F51。
- Memory 最新默认是通用过程摘要自动历史参考（generated_reference），稳定记忆才按需确认；旧六类只作场景/检索主题，不作写入必填目录。


## 2026-10-27 最新续接：F51 P1–P4 完成

- 用户明确授权继续到 P4；当前 Registry/Binding、统一 Runtime/SQL 调用台账、连续兼容组/共享容量和 Pi v2 桥接均已交付。最新代码路径从 [项目机制索引](docs/design/project-mechanisms.md) 进入，逐 P 摘要/选型/优势及证据统一见 [F51](contracts/F51.md)。此前交接中“仅 P1/其余未实施”均为历史状态。
- Pi 从完整 assistant message_end 封口一次批次，Python 独占 prepare/调度/执行；Pi sequential 消费缓存。原用户门禁、Job lease/generation、Case/BOM/来源接受及整批交付重验；F36 Artifact/F45 Observation 与对应台账 invocation/call/batch/index 一致。
- 0030 新 tool_execution、0031 新 tool_resource_slot 已在克隆库验证，未应用业务库。容量为加权占用记录，固定序原子多资源准入、受控续租；失租/崩溃只隔离，确认停止且 token 匹配才能释放。当前七工具无真实供应商资源声明，不代表已实现供应商 QPS 限流。
- 配置 config/product/tool-runtime-policy.json；strategy=serial 可消融并发，保留 v2/SQL/权限。每条 JSONL 16384 字节、每批/turn最多16调用；结果超限保留持久证据但终止交付，不截断/重执行。完整大证据渐进读取仍属 F52。
- 最终验证统一见 F51 最终记录；无真实 Provider/供应商/渠道调用，无业务库迁移、依赖/锁文件改动、git 暂存或提交，保留用户改动及 .claude/settings.local.json。
- 当前应由用户验收 F51；后续顺序为 F52 C1–C3，然后 F48 M1–M3。不要恢复旧实现，不把 Mem0/Context 方案当已上线。当前请求止于 P4，未启动下一 Feature。


## F52 最新开发续接

用户已授权按 Context/Memory 方案开发。C1 已交付 context_store.py 和 0032 的 Conversation/完整消息/Working State 私有存储，2离线/1克隆专项、164全量离线通过；业务库未迁移。当前仅激活 C2 每请求装配/v3桥接，C3/F48 未开始。C1 Python 多轮命令可独立调用；现有 Pi 自动完整消息采集接线由 C2 实施，不能把 C1 当已启用 Compiler。

## 压缩续接检查点：F52 C1 核查后（2026-10-27）

- 用户已批准按 Context/Memory 方案顺序开发，后续多次要求检查 C1 并继续；本次最新指令仅为压缩上下文，停止实现推进。F51 awaiting_acceptance；F52 in_progress；F48 not_started。不要求重新确认已经授权的阶段开发。
- C1 当前实现：src/product_api/context_store.py + 0032_f52_context_sources.sql；三张私有表（conversation/message/working_state）与受控命令。Python 多轮命令可调用；现有 Pi 自动完整消息捕获/每请求 Compiler 尚未接通，属于 C2。
- 最新 C1 修正：引用 kind/locator 必须一致；SQL 结果 payload 只允许 status/code/resultRef；checkpoint 引用 locator 核验；同范围显式跨任务恢复从原 checkpoint 的 Job 查历史证据，再重验当前来源，不错误要求证据属于新 Run。Working State 非法内容返回校验错误。
- 最新已执行证据：make f52-check 3 passed；make f52-integration-test 4 passed。克隆覆盖私有/跨组织隔离、配对/复合 ID/引用、幂等与游标竞争、关闭、旧 generation、完整事务回滚、实际子进程被杀后的未提交事务回滚、同对象显式恢复及不同 Case 拒绝。全量 make check 最后一次为上述修正前的164 passed，不能当最新完整门禁；最新 git diff --check 通过。
- C2 仅草稿且未接线/验收：context_runtime.py（配置/确定性预算与旧工具正文引用投影）、context_protocol.py（分块）、context_mapping.py（两种原生请求的语义映射校验）、context_session.py（捕获/准备/结束组装）、0033_f52_context_snapshots.sql、config/product/context-policy.json（enabled=false）。没有 C2 测试；Gateway/sidecar/Harness/Worker 仍为 F51 v2，尚不支持 v3。context_session.py 使用 context.role，但 HarnessContext 尚无此字段，接线前须处理。0033 仅草稿、从未应用；不可称 C2 完成。
- 下一步：先将 C1 最新证据同步 F52/模块/Feature（旧文字尚混有2专项/1克隆及“尚无实现”），运行全量 make check；再顺序完成 C2 的真实 Pi v3、每请求 prepare、快照/Gateway 映射与一次消费、授权/来源 epoch、取消与完整结束提交。C2 通过后再 C3，再 F48。不要同时激活多个单元。
- 所有新增迁移仅在自动删除的克隆库验证；业务库未应用0030–0033。无真实 Provider/embedding/供应商调用，无依赖/锁文件安装或 git 暂存/提交。不触碰 .claude/settings.local.json，保留巨大已有工作区及暂存改动。
- 当前 Memory 决策：共用通用过程摘要，SQL准确版本 + Mem0 infer=False检索投影，自动 generated_reference 历史参考；稳定命题按需确认。不恢复按角色逐字段提取。长期记忆、Context压缩、提交水位互相独立；Mem0 未实现。


## 最新续接：F52 C2 已实现（2026-10-27）

- 用户再次要求断线后继续 F52。已恢复 C1，补齐 C2 接线并完成本地验证；以本节和 F52 最新记录为准，旧“C2仅草稿/未接线”均为历史状态。F52 in_progress，C1/C2 已实现；下一单元 C3，尚未开始；F48 未实施。无需重新确认已授权的阶段开发。
- 代码入口：context_store.py（C1），context_runtime.py（保守预算/闭合旧工具引用缩减/分区记录/空 MemoryPort），context_protocol.py + pi_sidecar/context_protocol.mjs（受限分块），context_session.py（每请求捕获/准备/终态提交），tool_bridge.py/harness.py/worker_main.py（按配置固定v2/v3），context_mapping.py/gateway.py（原生映射校验及同事务快照接受+exchange写入）。Specialist 角色已写入 HarnessContext；显式续接禁止跨角色读取旧 Working State。
- 0033 实际新增 context_snapshot、source_epoch 及 model_exchange关联列。context_prepare/accept/finish/abandon/required 受控函数；粗粒度组织来源epoch触发器使未消费快照失效。撤权后隐藏RLS会话也不能靠移除Context header走旧路径。退出取消自己的prepared快照，失租仍由原租约门禁拒绝。Replay消费不伪造Provider exchange。
- 默认 config/product/context-policy.json enabled=false，既有入口仍v2；克隆测试注入enabled=true验证v3，不改真实.env。v3每次发完整规范化历史（1MiB总传输上限），尚未优化增量messageRef；16KiB工具结果超限可换原持久引用，装配前从已接受结果恢复；最新强制证据仍过大则context_overflow。没有C3渐进读或模型摘要，不要宣称无限历史可用。
- 验证：f52-check 23 passed；f52-integration-test 12 passed；f51-integration-test 11 passed；make check 185 passed、33 deselected、2条既有警告；git diff --check通过。F52覆盖真实Pi Responses/GLM五角色多次请求、每请求快照/exchange关联、关闭commit、Replay、取消准备、epoch/旧generation、缺失ready/正文和Schema篡改/重用、原错误参数及清理。测试均为固定本地报文+自动删除克隆库，无真实模型或embedding。
- 仅新增0032/0033在克隆库执行；业务库未应用0030–0033。未安装依赖/锁文件、未暂存/提交、不触碰.claude/settings.local.json，保留原有巨大脏工作区。
- C3开发前读取contracts/F52.md、CONTEXT_RUNTIME.md、BL13/DC18/DC21；补闭合前缀摘要/coverage和依赖失效、L0/L1/L2授权渐进读、summary失败/超限/否定未知保持，最后再F48。长期记忆继续通用过程摘要默认 + SQL版本 + Mem0 infer=False投影，不恢复逐角色提取器；Mem0 SDK尚未接入。


## F52 C3 完成与 F48 M1 启动（2026-10-27）

- 用户授权 C3 后继续 F48。C3 已实现：context_compaction.py、0034、Store/Session/Compiler接线；28离线/13克隆、190全量离线通过。保守逐字段落去重，不改写否定/时点/条件/理由；首尾完整单元保留，过大明确失败。compaction独立列/覆盖manifest与epoch不覆盖来源，不推进长期提取游标。Reader重验来源/私有权限、分级分页和读取预算；单个不可分片段超限报错，不截半个工具单元。
- F52 awaiting_acceptance，本地实现完成不代表部署或业务质量验收；Context默认关闭，业务库未迁移0030–0034。新活跃单元仅F48 M1，先验证锁定实际Mem0的本地add/search/擦除，再开发SQL摘要版本/持久工作项/带引用注入。M2/M3未开始；不做角色定制提取，不调用真实付费模型/embedding，不碰用户配置或暂存。

## F48 最新续接：M1 合成闭环核验与生命周期修正（2026-10-27）

- 本轮用户要求检索 F48 进度并继续。恢复发现 M1 的 Adapter/Store/Worker、0035、Context 接线和测试已有草稿，F48 Contract/模块首页仍停留“尚未实施”；现已同步，F48 保持 in_progress，M2/M3 未开始。
- 本轮代码修复：共用摘录器按原顺序保留重复的最终否定；memory_guard 拒绝已索引较新来源范围后的旧批次接受/发布（不调用 embedding）；纠错拒绝撤回/删除/过期/来源不可读版本，避免旧版本重新激活。新增离线时序和克隆乱序/软硬忘记防复活回归。
- 本轮证据：make f48-check 9 passed；f48-integration-test 7 passed（实际锁定 Mem0/Qdrant/SQLite、回环 embedding、克隆 PostgreSQL、真实 Pi）；make check 199 passed、41 deselected；f52-integration-test 13 passed；f51-integration-test 11 passed；git diff --check 通过。Qdrant 本地 payload index 提示不代表服务端性能验证；离线一条既有 Starlette 弃用提示。
- 0035 精确包含 conversation 的 memory_enabled/memory_denied 两列与 memory_source/version/evidence/work_item/projection/delete_request 六表、索引/RLS及受控命令。仅克隆重复应用验证幂等；业务库未迁移0030–0035。本轮未安装依赖、未调用真实模型/供应商/渠道、未暂存/提交，未触碰 .claude/settings.local.json。
- 当前边界：默认关闭且 syntheticOnly；摘要器是原顺序有界摘录，超过来源32KiB/正文8KiB隔离；显式偏好/确认等目前为 Python 命令。Mem0 infer=False 唯一发布后端，SQL 版本/来源权威；旧 F36 路径未切换。硬删除清本地隔离域及 SQL/直接 Context 后仍返回 backup_pending，不能宣称备份擦除完成。
- 下一实施仍只做 M1：先实现受控通用摘要器、超长闭合来源分块与质量验证，再补来源失效/取消并发、消费后的派生删除/禁止再加工、持久计量及分类保留清理。七天版本查询有效期不是自动物理清理；备份清除和恢复防重生未验证。未满足这些门槛不标 M1 完成，不提前做 M2/M3；接口/完整缺口见 contracts/F48.md 与 MEMORY_RUNTIME.md。

## F48 最新续接：M1 闭合增量来源与覆盖清单（2026-10-27）

- 用户要求继续 M1。本单元已完成新增水位的闭合分块与 SQL coverage，不进入 M2/M3。0036_f48_memory_ranges.sql 追加 source.start_ordinal、version.summary_manifest；更新 capture/guard/accept/indexed。保留0035历史，不创建新的记忆后端或依赖。
- 现在只捕获新提交区间，以完整 message_unit_id 按存储行JSON约6144字节打包；不能拆分的单元单独保留，由32KiB输入/8KiB输出限制决定是否隔离。Store精确读起止区间并核hash；摘要复核ordinal连续、complete及工具配对，保留工具参数/失败。L1返回SQL生成的策略/source/hash/区间/逐单元coverage；召回来源含startOrdinal。
- 同会话的非重叠摘要可以同时ready，倒序完成不丢前块；只由完整覆盖的新区间退役旧投影。旧重叠前缀仍拒绝。忘记沿用同会话/assertion保守范围，实际SQL正文和Mem0投影均已验证清除，仍backup_pending。
- 本轮执行：f48-check 14 passed；f48-integration-test 9 passed，随后强化多块SQL/投影删除断言的long_source专项1 passed；make check 204 passed、43 deselected；f51-integration-test 11 passed；git diff --check与features JSON校验通过。Context回归状态见下方最新补充。
- 初次新增长来源测试的硬删失败定位为克隆漏装0027/model_exchange，已补齐前置并恢复标准错误边界；未降低删除断言。Context并行回归首跑12 passed/1 failed，为Replay结束等待5秒超时，正在独立串行复核；没有修改超时或实现来掩盖问题。
- 0035/0036仅在自动删除克隆中重复应用，业务库未应用0030–0036。默认关闭且只准合成；没有真实Provider/模型/embedding/供应商/渠道调用，没有依赖安装或git暂存/提交，保留原有改动与.claude/settings.local.json。
- M1仍未完成：当前是共用有界原文摘录，不是模型语义摘要。后续先接独立Memory加工的模型授权/预算/持久usage（现有Gateway绑定业务Job，不能借已结束Job旧租约），再补模型摘要质量、来源/取消并发、消费后派生删除、分类保留与备份恢复防重生。旧F36读取不切换。以contracts/F48.md和MEMORY_RUNTIME.md为准。
- Context最新补充：待其他数据库回归结束后，未改代码/断言/超时，独立完整执行make f52-integration-test为13 passed（43.64秒）。首跑5秒超时未复现，根因未确认，不声称修复。当前没有运行中的本轮检查；上述pending文字是本节过程记录。

## 2026-10-28 F48 M1本地交付，继续M2→M3

- M1本地合成实现完成待验收，0037独立计量、0038依赖删除/取消/保留及恢复屏障；详见F48最新矩阵。f48-check 19 passed；f48-integration-test 29 passed, 1 deselected；make check 209 passed, 63 deselected；f52集成13 passed，f51集成11 passed。0035–0038仅克隆，未迁移业务库；backup_pending仍保留。
- 用户已授权顺序推进M2、M3直至结束；当前仅M2活跃，M3在M2验证后实施。无真实模型/渠道调用、git暂存或提交，保留原有改动及.claude/settings.local.json。

## 2026-10-28 F48 M1–M3本地完成待验收

- 已依用户授权依次完成M1、M2、M3，没有活跃实施单元。F48=awaiting_acceptance，用户验收/真实上线另行决定；完整矩阵见contracts/F48.md文末。
- M2：0039时间/条件及私有版本关系，0040旧F36导出复核/幂等导入/投影对账与显式单读路由。撤权导出正文为空，新旧路径都遵守删除屏障；旧表/写命令保留，没有实际数据切换。
- M3：0041 memory_derivation、memory_dream_batch、temperature；复用原Memory工作项/generation/预算/对账。lossless-dream-v1只精确块去重/拼接；≤30输入/8000字节，按原始支持幂等，默认关闭。同日50新增或次日有新材料并满足收益才排队。复盘候选仅原句、等待确认；冷层降排序。无损合成样本覆盖从5/6升到6/6，不代表真实模型/生产收益。
- 删除同时覆盖未接受的派生输入副本和已发布图；先deny/cancel，清实际SQL/Qdrant/SQLite并按合法存活者重建。恢复最新独立ledger后须清全部requested才解除blocked；未知备份仍backup_pending。
- 验证：f48-check 20 passed；f48-integration-test 38 passed,1 deselected；make check 210 passed,72 deselected；最新Dream/终态专项3 passed，旧数据撤权导出2 passed。最终Context回归结果见下方补充；M1阶段Context13及F36/F45/F49/F50业务链11通过。git diff/JSON检查通过。
- 曾一次普通偏好发布返回reconciliation_required，发生在Dream排队之前；原测试未含code。增加report后串行3专项通过，原因未确认，不能报修复；没有改断言、超时或重试来变绿。一次诊断缩进编辑收集失败已纠正。SDK Qdrant/Starlette既有提示仍在。
- 0035–0041仅自动删除克隆；业务库未应用0030–0041，无真实Provider/付费模型/供应商/渠道请求，无git暂存/提交，保留用户改动与.claude/settings.local.json。下一步为用户验收和另行授权的部署配置/备份清单/迁移，不自动激活真实Memory。

最终Context独立串行回归：`make f52-integration-test` **13 passed**（43.18秒）。最后ruff、features JSON与`git diff --check`通过。没有仍在运行的本轮检查。
