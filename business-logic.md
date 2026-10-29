# SupplyAgent 业务逻辑与变更影响索引

版本：2。更新：2026-10-27。

本文是本项目特定业务规则的集中正文。`spec.md` 负责产品目标/架构，Feature Contract 负责实施范围与验收；字段、身份、交接与持久语义见 [data-contract.md](data-contract.md)。实现状态以 [features.json](features.json) 为准，目标规则不代表代码已实现。

修改策略时先找到 BL 编号，核对本条的配置、生产者、消费者和测试；涉及数据含义/状态/来源变化时，同时检查对应 DC 编号。合成业务参数不是生产承诺，不把示例数值硬编码进模型规则。

## BL01 四类业务目标与当前边界

| 场景 | 业务流程与交付 | 实施归属 |
|---|---|---|
| 销售运营 | 用户选择范围 → 确定性发现跟进缺口 → 事实/政策及条件供应调查 → 可审阅操作清单 → 用户授权后的 CRM 操作 | 只读跟进节点已有；完整入口和写入后续 F32/F41 |
| 情报问答 | 用户选有权限商机并提问 → 并行证据调查 → 商机诊断 → 中文答案、引用、未知、冲突 | F45–F47 的固定只读模板已实现待验收；连续追问和语义 Judge 未实现 |
| 客户沟通 | 结合会话和有效知识 → 生成可编辑回复 → 人工审阅 → 受控发送或转人工 | 草稿/发送/交接的完整产品流程后续 F37/F41，不以模型文字代表发送成功 |
| 主管诊断 | 用户手动选团队/漏斗范围 → 汇总任务、商机和执行记录 → 异常及逐项修复计划 → 审校后执行 | 跨漏斗聚合/修复计划仍待实现；当前不自动创建巡检 Mission |

当前只交付 Web；常驻 Worker 为默认，cron 仅可领取已有任务。具体模拟故事与异常变体见 [业务故事](docs/scenarios/2026-10-23-electronics-business-stories.md)，故事是需求/测试素材，不是正式业务事实或实现证据。

影响：`app.py`、`intelligence.py`、`mission_runtime.py`、`frontend/src/app/page.tsx`；F32/F33/F36/F37/F41/F45。数据：DC01–DC03、DC08。

## BL02 权限与业务主体

- 组织隔离是底线；业务范围还需要客户/商机/子对象 scope、知识 ACL、来源撤销校验。不可见记录不能推断为不存在。
- API 验证 Auth token 后在同一事务重新确认成员、角色与组织。组织 Cookie 是选择提示；模型/前端传入的 actor 或 scope 不是授权依据。
- 情报工具按原提问者当前权限读取，不借 Worker 技术身份扩大权限。Worker 需要 maintainer 且 `runtimeWorker=true`，不等于拥有全部业务可见性。
- 当前供应事实人工写入需要 `supply:propose`：organization_admin、buyer、hardware_engineer 可用；其他角色不因能读而能写。CRM 情报入口要求当前 CRM 能力，角色完整映射由 `rbac.py: ROLE_CAPABILITIES` 维护。

影响：`auth.py`、`tenant.py`、`rbac.py`、`app.py`、`crm.py`、`intelligence.py`、各领域 RLS；测试 `test_product_api.py`、`*_integration.py` 的跨组织/同组织 scope/撤权分支。数据：DC01、DC11。

## BL03 Specialist 判断分工与工具绑定

- 当前情报模板：客户事实和业务政策并行；存在合法 Case 关联时增加供应风险；启用商业节点时增加报价时效；最后由商机诊断读取前序结果回答。不是动态 Planner。
- 客户事实只解释授权 CRM 事实；业务政策返回有效知识与缺证据；供应风险解释确定性投影；报价时效解释确定性状态；诊断保留前序未知、冲突和失败。
- 工具分别为 `crm.opportunity_facts`、`knowledge.question_evidence`、`supply.bound_impact`、`commercial.quote_validity`、`mission.claims`；角色只收到精确允许的 R0 集合。SQL、算术、检索、引用校验不是新增 Specialist。
- F36 的 `opportunity_next_action` 另保留固定 Mission 模板、独立 review、唯一父汇总和不可执行 ProposalDraft；与 F45 不是同一结果/验收路径。同一 Mission 父提案汇总并发上限为 1。
- 当前引用校验不等于语义支持校验。F45 结果显式 `needs_review`；冲突裁决、反证补证、局部重规划和任意 MCP/Skills 装载后续设计。

影响：`specialists.py`、`specialist_runner.py`、`mission_runtime.py`、`intelligence.py`、`model_tools.py`、Pi sidecar；测试 `test_product_api_specialists.py`、`test_product_api_mission_runtime.py`、情报/商业/Responses 集成。数据：DC03、DC05–DC08。

## BL04 跟进缺口判定

规则配置唯一来源为 [followup-policy.json](config/product/followup-policy.json)，当前为 synthetic v1：activeStages 为 lead/qualified/proposal/negotiation；要求商机负责人、任务负责人和未来任务；最多 32 个 Case 零件；业务时区 Asia/Shanghai。

- 非活跃阶段不产生跟进信号；只检查可见、`status=open` 的任务。
- 商机无 owner → `opportunity_unassigned`；任务无 assignee → `task_unassigned`。
- 任务 due_at 缺失 → `task_deadline_unknown`；due_at **小于**分析时点才是 `task_overdue`，等于时点不算逾期。
- 没有 due_at 大于等于分析时点的开放任务 → `no_visible_future_task`；不能因此断言组织内没有未来任务。
- 新增未来任务只消除对应缺口，不自动完成旧逾期任务；分析时点不代表历史数据库已被重建。

影响：`business_nodes.py: followup_signals/followup_context`、配置和 CRM owner/assignee/due_at；测试 `test_product_api_business_nodes.py` 及 F46 集成。数据：DC04、DC09。

## BL05 供应影响投影与未知

- 数量使用 Decimal；库存开盘可用量取 on_hand − reserved。缺少库存/数量，或存在未知/非零且未对账的聚合在途量时，不能给出精确投影。
- 只将 `supply_receipt.status=in_transit` 且时间/数量明确的明细作为到货事件；需求时间/数量缺失保留 unknown。
- 时间线只纳入日期大于等于分析日的有效事件；同日先入库到货再扣需求，同类型按引用稳定排序。已知事件可形成部分投影，缺失明细仍保留 unknown，不代表完整供需。确定性计算可用量和首次缺口，不让 LLM 补数。聚合在途与明细的权威归属仍是 F43 未决事项，不重复计入，也不把保守风险称为精确缺料日。
- F46 手工需求路径覆盖接单时 Case 的全部指定料号；实际 Case 关联或料号范围变化，旧任务拒绝继续，不静默扩大读取。
- Agent 不排产、不改工单或生产计划，只提供影响分析。

影响：`supply.py: calculate_impact`、`business_nodes.py: validate_case_parts/case_supply_context`、F39/F43；测试 `test_product_api_supply.py`、供应/业务节点集成。数据：DC04、DC09–DC10。

## BL06 BOM 版本与本商机覆盖范围

- 产品和 BOM 是组织业务对象；绑定固定 BOM 修订与 binding version，不自动改用“最新版本”。发布后内容不可变，纠正用新修订，允许显式撤回。
- 每行需求 = 商机绑定 units × quantityPerUnit；同 MPN 汇总，但同 MPN 不同厂家视作歧义并拒绝。重复 lineKey、非正数或非有限数量拒绝。
- 商机绑定的修订、数量或 required_on 变化推进 version；不能删除后重建重置版本。排队后改绑拒绝旧调查，历史结果需显示绑定变化。
- BOM 与接单 Case 的 MPN 集合必须一致。商业路径以 BOM 派生需求替换手工需求，不双重叠加；缺 BOM 不伪造回退需求。
- 投影只回答本商机在当前可见库存/到货下的覆盖，不代表库存已分配、全组织可同时承诺、已锁货或允许排产。

影响：`commercial.py: bom_requirements/bound_bom/bom_supply_context`、`0026` 约束、`intelligence.py`；测试 `test_product_api_commercial.py`、商业集成。数据：DC04、DC09–DC10。

## BL07 报价时效与商业含义

`quote_validity()` 按以下优先级判定：撤回 → BOM 不匹配 → 尚未签发生效 → 被当前可见且已签发的新修订替代 → 有效期未知 → 已过期 → 有效窗口内。

- `valid_until <= as_of` 即过期；未来修订不能提前替代已签发报价。比较按 reference + quote_kind 分组，只覆盖可见修订。
- supplier_offer 必须指向固定 BOM 中存在的行；customer_offer 与 supplier_offer 不因来源不同就天然矛盾。
- 缺价格/币种、有效截止、锁货或确认到货信息均显式保留。有效报价不等于已锁货、确认收货或获准采购；预计发货不等于确认到货。
- 报价不加入供应确认收货；报价/BOM ACL 变化、撤回或改绑会影响旧结果可见性与有效性。

影响：`commercial.py: QuoteFact/quote_validity/quote_context/source_is_current`、`0026`、`intelligence.py: read_answer`；测试商业单元/集成与 GLM 报价 fixture。数据：DC04–DC06、DC09。

## BL08 证据与知识

官方板卡/器件资料只支持技术说明及明确版本适用性，不证明实时价格、库存、交期或替代审批。synthetic 故事/种子/政策不能标成真实客户事实或 official 制造商数据。

知识按不可变文档版本、chunk、locator/hash 引用；先权限过滤再检索，返回后重验当前 ACL/撤销/撤回。缺证据输出未知，不让旧摘要、模型结论或有效引用自动充当业务真相。

影响：`knowledge.py`、`knowledge_import.py`、`crm.py`、`commercial.py`、`intelligence.py`、知识 demo manifest；测试知识导入/ACL/撤销与情报集成。数据：DC05–DC06、DC11。

## BL09 Memory 选型与治理边界

2026-10-27 用户确认 F48/F52 完整目标；以下为治理策略，当前本地实现、验证与启用限制见F48 Contract，不代表已部署。主选 Mem0 OSS 的检索投影/embedding/语义检索，事实抽取仅为按需补充；SQL 保存经过治理的准确记忆版本、来源、权限、有效期、关系和生命周期，Mem0 保存候选加工与可重建检索投影，不另建 SQL 有效记忆向量/全文搜索。正式库存、价格、BOM、报价、审批仍归业务库。

### 默认过程摘要与按需稳定记忆

默认对已提交的完整交互片段生成通用过程摘要，不按角色/场景预定义提取方向，也不要求拆出“策略理由”等业务字段。正文自然语言保留背景、进展、结论及理由、纠正/否定、不确定性与未决事项；元信息只要求来源区间/hash、组织/原用户/业务绑定、时间、权限和版本。角色差异放在读取时的任务检索与 Context 选择。

| 层次 | 准入与用途 | 权威 |
|---|---|---|
| process_summary（默认） | 通过来源完整性/权限/PII/摘要质量门槛后自动进入可检索历史参考，不逐条人工审批 | 标为 generated_reference；描述曾经的交互，不当作已验证事实、当前状态或授权 |
| stable_memory（按需） | 显式要求记住的偏好，或拟提升为稳定规则/惯例的命题；人工或确定性来源确认后使用 | 标为 confirmed；仍受当前来源/范围/有效期约束，不能覆盖领域事实或组织规则 |

原先 personal/entity preference、decision/task episode、business convention、procedural lesson 保留为业务样本和检索主题，不是强制分类目录，不为每角色新增提取器。默认摘要无需强制填写 subject/predicate/value；用户明确的偏好命令可以结构化处理，不能让模型自动分类成为整个写入链的前置。

本人私有偏好由本人确认，共享稳定规则初始仅 organization_admin 在来源与对象授权通过后确认；过程摘要不经过此人工确认流程。摘要的自动可检索不改变其生成参考属性。业务策略/承诺/授权永远不能从摘要推断；低置信或来源缺失先隔离，不靠管理员批量审批弥补来源缺失。

### 捕获与加工

每个已接受用户轮次/阶段产物提交新增来源水位和 outbox；显式记住/纠错/忘记优先，关闭会话/任务终态及时提交尾批。commit 只承诺来源与待加工任务持久化，不等于已索引/可用；主任务不等待 Mem0，但 SQL 提交失败不得谎报已记住。空闲不是业务结束，内存队列不是唯一恢复路径。

默认由受控通用摘要器处理完整来源片段，SQL 保存准确摘要版本，以 Mem0 infer=False 建立可重建检索投影，不再次事实抽取改写摘要。只有按需稳定记忆分支才可用 Mem0 infer=True 提取候选，并隔离 candidate 与已发布存储。前置过滤在任何外部模型/embedding 前执行，后置检查摘要/候选。摘要提示词和 custom_instructions 仅为质量约束：密钥/能力/密码不进入候选，必要联系人 PII 优先从 CRM 取引用，synthetic 不进入真实 memory；外部文本/助手推断不能生成权限、数值、承诺或正式政策。否定、主体、时间、条件不能被抽取/摘要丢掉。检测不保证绝对完整，无来源支持则不发布。

### ADD-only、来源合并与冲突

ADD-only 约束摘要/稳定记忆正文版本追加，允许状态/关系变化、当前投影替代以及隐私删除。过程摘要按来源区间/内容幂等，不因主题相似直接折叠历史；重叠覆盖记录来源区间。原文保留在其授权来源中；原文、过程摘要、语义记忆分别处理，不将完整聊天直接当有效事实库。

| 情形 | 决策 |
|---|---|
| 同事件重复 | 来源区间/hash/策略版本幂等跳过 |
| 同主体、命题、值、条件与时点一致 | 追加证据边，不丢新来源 |
| 仅语义近似 | 比较主体/谓词/时间/条件/范围，相似度不能直接签发合并 |
| 互补内容需综合 | 追加综合版本及 merged_from，保留来源与限定条件 |
| 本人明确纠正同范围偏好 | 新版本原子替代当前投影，不改写旧正文 |
| 同时点同条件矛盾 | 建立冲突组；领域权威可确定时按领域规则，否则补证/审阅，不自动最新优先或平均 |
| 不同时期变化 | 记录有效时间演进，不互删历史 |
| 来源改变/撤回 | 先禁止相关版本使用，转待重验/撤销，不能只刷新索引 |

### 检索、注入与故障

意图先由任务/角色和绑定对象约束：当前库存/报价/审批/发送查领域工具，政策/技术资料查知识，偏好/历史决定查 memory，步骤/“继续”查 Working State/近期消息。指代不明确时澄清，不以全库最相似实体猜对象。

namespace/filter 来自可信原用户/组织/对象，后端预过滤后才可将正文给 reranker，返回后重验当前权限、来源、状态/有效期和准确版本。缓存只复用候选，每次注入仍重验；撤权/删除推进 epoch，不能用缓存 TTL 当撤权承诺。按完整块选择，保留时点/条件/否定与冲突；检索分数不是置信度。注入带 memory version、generated_reference/confirmed 属性、适用范围及来源；业务结论仍回查原始/当前证据。

后端断开时明确未使用长期记忆，历史依赖问题标出缺失；不静默切换形成双重权威。远端写成功、本地未确认时先对账，Runtime/SDK/Job 不叠加盲重试，不承诺跨系统 exactly-once。

### 复盘、巩固与遗忘

任务终态及时提交通用过程摘要任务；Dream 由有新增材料的持久 Job 整理跨片段历史/冲突关联。整理后的过程摘要按通用门槛自动成为历史参考；只有提升为稳定经验/规则的候选才走确认。派生链保留，反复转述同源不增加独立证据。无新材料不调用模型；默认关闭，消融证明收益后启用，不永久删除该目标。

soft forget 用于替代/撤权/撤回/过期/隔离/暂不采用：服务端提交 deny 起阻止新检索/注入，再更新索引和派生物。稳定偏好到明确变更/撤销；任务约束在阶段结束退出当前集合；低频只降排序/入冷层，不等于错误，关键否定不能因少用消失。

hard delete 用于有权明确删除、保留策略到期、错误敏感收集：先 deny，再清正文/向量/候选/history/messages/entity、缓存及依赖摘要/快照等，逐目标验证。删除请求明确原文/业务记录/审计范围；不能借不可变审计保留应删正文。多来源保留仅允许独立合法支持的内容，删除来源的独特信息须消失。tombstone 阻止旧 outbox/Dream/备份恢复再生，不保存明文 PII 作为防重生标记。

备份按明确保留策略/密钥销毁处理，恢复先重放 deletion ledger；仍有备份副本时显示待清除，不能宣称全部物理删除。已外发 Provider 的内容不能撤回。分类保留期由组织配置，未配置不能接真实数据；不自动继承框架永久保存。

### 后端、切换与边界

Python 是唯一受控 Mem0 Adapter；Node 不持客户端/密钥。默认关闭，优先自托管；锁定 SDK/提取/embedding/存储、数据去向与成本后按 G5 启用。Mem0 无法兑现安全预过滤、候选隔离、发布对账或完整擦除时，F48 验收失败，按同样本评估 MemMachine 等替代；不以私有 fork 掩盖成本。

旧 memory.py/long_term_memory 保留到导出、状态映射、来源复核、投影对账和单一读取切换通过；回滚不能恢复已撤销/删除内容。F48 范围和验收见 Contract，接口/后端限制见 [Memory 模块](src/product_api/MEMORY_RUNTIME.md)；数据 DC19/DC20，Context 消费见 BL13/DC21。

## BL10 提案、审批与外部动作

CRM 写入、发送、采购提交由 Harness 门控；业务提案固定目标版本、内容 hash、证据与有效期。人工批准不是永久授权，执行前重验当前权限、目标版本、内容、opt-out 等约束。结果未知不能盲重试为成功。

当前已实现F41人工customer_update及F37 fixture消息共用的Proposal/Approval/Execution/Job闭环；F36 ProposalDraft仍不可执行，旧crm_proposal不演变成第二条执行权威。真实采购/WhatsApp/飞书和Agent自动R1工具仍未接入。

- 人工证明仅接受提案者创建、F40 hash重算通过的manual_action_evidence；不把模型结论或未接线的外部派生证据自动当成执行依据。内容、目标版本、证据、到期、请求者与服务revision绑定审批；禁止自批。消息正文/目标密封落库，只有当前请求者或可审批且有对象scope的主体可解密审阅。
- 批准只是短期数据库审批收据，不是执行完成。Worker必须重验双方当前权限/scope、内容hash、目标版本、opt-out、到期、证据、enabled与revision；缺政策草稿revision=0不可因后续启用复活。服务政策变化须递增revision，审批时长由max_approval_seconds配置。
- customer_update的本地SQL效果与账本同事务；同一个logical_action_id重投不再更新客户。消息先提交发送意图，再受控调用Adapter；未知结果只查状态，不能盲发。accepted与delivered分开，已发出的效果不能由取消撤回；迟到可信回执记录事实，不把已取消Job改成完成。
- 当前仅fixture通道允许执行，真实通道名拒绝。独立SQLite和自有HMAC回执用于本地验证，不冒充WhatsApp/飞书协议；G5仍约束真实连接与发送。

影响：F32/F37/F41、actions.py、channels.py、app.py、Worker及账本；数据：DC08、DC14、DC22；源码/运行说明见src/product_api/ACTIONS_RUNTIME.md。

## BL11 当前模型与数值责任

当前免费路径固定 zhipu / glm-4.7-flash，经 Pi 的 OpenAI 兼容 Chat Completions；保留 OpenAI Responses 路径，不引入自制多厂商消息转换。策略由服务端确定，模型不选择任意端点/型号、不自动回退付费型号。Key 只在 Python 解析。

真实模型可用性与业务语义质量分别验证；Replay、合成报文和真实调用分开标记。计量使用 Provider usage 与受信价格，缺 usage 不按零费用释放预留；模型不能签发数值、权限、引用或执行授权。

影响：`gateway.py`、`glm.py`、`model_tools.py`、Pi sidecar、`app.py`；测试 gateway/GLM/Responses。数据：DC07、DC10。

## BL12 工具绑定、执行与失败策略（F51 目标）

本节策略已由 F51 P1–P4 实现并提供本地/克隆验证，等待用户验收；0030/0031 未应用业务库。当前仍仅开放 R0，状态见 features.json。

- 角色只接收当前任务、原用户权限与业务对象共同允许的工具子集；执行前重验原用户权限、绑定版本及活动 Job lease/generation。Worker 技术身份不能补足原用户授权。准备快照不是永久授权；结果接受前仍需当前门禁。
- 副作用声明（只读/内部写入/外部动作）、并发兼容性及资源约束相互独立。只有受信任的服务端声明决定调度；MCP readOnlyHint 等提示仅供接入配置，不能授权执行或并行。当前工具仍限 R0。
- 并发只允许连续且彼此兼容的调用重叠，保留非兼容调用的顺序屏障。可并发不能证明可越过前序写入，也不承诺一致数据库快照。调用之间存在数据依赖时由模型分轮或 Mission 显式编排；Scheduler 不推导业务依赖。
- Mission 保留 Specialist 的必需/可选分支、汇总、失败与阻塞策略；底层调度不得把失败、unknown 或引用缺失改成成功事实，不将新错误自动认定为已有业务已修复。

| 情况 / 稳定 code | 对模型及业务的处理 |
|---|---|
| 参数错误 invalid_arguments、未知名称 unknown_tool | 返回有界反馈，允许在调用预算内用新 callId 修正；无模糊名称修复、全局目录或其他工具描述 |
| 合法查询无数据 | 正常结果并明确 unknown/不可确认；不可见不等于不存在 |
| 外部服务暂不可用 service_unavailable | 返回明确失败；Mission 按既有必需/可选分支策略处理，不将失败当作证据 |
| 工具已注册但角色未绑定 tool_not_bound | 不执行；模型仅见与 unknown_tool 相同的不可用反馈，私有记录可区分原因，不泄漏目录 |
| 原用户撤权 permission_revoked、业务绑定失效 binding_invalid、Job 失租 lease_lost | 停止当前执行路径、取消未完成兄弟，不交给模型换参数绕过 |
| 取消 cancelled、截止 deadline_exceeded | 停止准入和当前路径，不接受迟到结果；沿既有 Job 终止映射，不新增业务成功状态 |
| 协议破坏 protocol_error、重复 ID duplicate_call、持久化失败 persistence_failed、未分类内部错误 internal_error | 系统失败并终止 turn；不作为普通业务证据，不泄漏原始异常 |

工具反馈不是自动重试：模型修正是新调用并消耗预算；网络重试归既有调用策略，Job 重试推进 generation；Runtime/Scheduler 不叠加重试层。外部动作恢复与重复副作用治理仍由 F41 后续处理。

影响：Harness、Registry/Runtime/Scheduler、specialist_runner、intelligence、F36 工具包装、Pi；测试工具公共接口、桥接及 F36/F45/GLM/Responses 克隆回归。数据：DC15–DC17；架构：spec §2.7。

## BL13 短期状态、Context 装配与压缩（F52 目标）

- Transcript 是追溯/恢复来源；Message Buffer 是近期投影；Working State 是任务目标/约束/步骤/已接受证据/未知/失败/待人工及下一步；长期 memory、每请求 ContextSnapshot 和有损 CompactionCheckpoint 分开管理。
- Conversation 可关联多个任务，显式续接重验参与主体和当前对象权限；默认不共享私人会话。Specialist 只收到自身角色/范围内的交接，不共享全量 history。当前单次问答保持默认独立会话，不因新增模型悄悄串历史。
- 工作状态身份、步骤/状态和数值/授权由确定性来源维护；模型仅提出验证后的结构化增量。阶段接受即 checkpoint，恢复重验当前 scope/来源/lease；不恢复 Pi 内存或重放半段工具执行。
- 一条 assistant 工具消息与全部终态结果构成不可拆单元；活动批次固定保留。近期 buffer 同时受消息单元/token 限制，不能按最后 N 条剪出悬空结果；取消/失败不伪造成功来凑配对。
- 每次模型请求由 Python 装配并重算实际模型/部署输入、output/reasoning、Schema/协议开销和余量；当前规则/目标/绑定、关键事实与未知、有效工具配对优先。长期记忆是软配额，不能挤掉本次问题依据。
- 执行指令按受信系统/组织规则与当前授权处理；事实按领域权威、有效时点/范围、确认状态和来源处理。当前业务事实优先于陈旧 memory，正式政策优先于个人惯例；本人新偏好不能推翻组织策略。不统一采用“最新消息胜出”。
- 引用注入带类型、版本、范围/时间、确认/未知/冲突和来源；外部工具/记忆正文在证据区，不提升为系统指令。渐进读取 L0 提示→L1 有界内容→L2 原始授权片段，每次鉴权、分页及计费；派生摘要权限不宽于其来源。
- 溢出先去无关/重复/过期块、缩旧闭合工具正文，再摘要已闭合前缀；保留目标/约束、事实引用、未知、失败、下一步和覆盖水位。摘要失败保留 Transcript；强制内容仍超限则拆任务或 context_overflow，不截掉权限/核心任务。
- commit、compaction 与长期 extraction 三条进度分开。移出上下文不等于要 add；Context 压缩摘要不自动确认为稳定记忆；默认长期过程摘要从原始来源独立生成并标为历史参考。会话关闭/任务终态及时提交来源/状态/outbox，不等待长期索引。
- 模型切换、新 Schema 和大结果到来重算容量；授权/来源变化使旧快照失效，不能沿旧摘要/缓存绕过。发送前最后重验只能阻止未发送请求，不保证撤回已经外发内容。

影响：Harness/Sidecar、intelligence、SpecialistRunner、Runtime、Gateway、未来 ContextStore/Compiler；范围/验收 F52，接口与算法 [Context 模块](src/product_api/CONTEXT_RUNTIME.md)，数据 DC18/DC21。

## 变更影响判断

| 想改变的策略 | 首先修改/确认 | 必须复核的下游 |
|---|---|---|
| 阶段、跟进要求、时区、零件上限 | BL04 配置与语义 | 业务信号、as_of、观测政策快照、边界测试、prompt 解释 |
| BOM 合并/改绑/需求口径 | BL06、DC04 | SQL 不可变性/版本、接单 scope、供应投影、旧引用有效性、引用答案 |
| 报价替代/到期/到货定义 | BL07、DC04/DC09 | 报价读取、确定性分类、供应计算、Specialist 解释、历史答案 |
| 用户/角色/对象可见性 | BL02、DC01/DC11 | API、Worker 原用户读取、知识/记忆过滤、旧产物可见性、RLS 测试 |
| 增减角色/工具 | BL03/BL12、DC03/DC05/DC07/DC15–DC17 | 角色编排、工具 schema/别名、Pi、Observation、父交接、Trace/Eval |
| 记忆/Context 使用 | BL08–BL09/BL13、DC03/DC11/DC18–DC21 | 来源版本、候选确认/撤销、同步、输入快照、输出和质量评测 |

规则改变不能只改 prompt。改变持久含义须追加 schema/格式版本或 migration；改变解释而不改变事实也要保留实际 prompt/config 标识。尚未实现的策略不能由本索引自动获得实施授权。
