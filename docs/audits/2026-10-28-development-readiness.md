# Feature 测试汇总与当前入口联调前置清单

> 本文保留实施前盘点。后续授权开发的 F38/F33/恢复/F41/F37 切片与最新验证见 [实现摘要](2026-10-28-implementation-summary.md)；下表旧状态不覆盖最新 Contract/features.json。

日期：2026-10-28。供用户自行联调和评测使用；本轮只整理文档，不执行端到端测试、启动服务、迁移数据库或调用真实模型。按用户再次提出的完整范围，第6节补充面试用错误恢复总览，不改变代码行为。

依据：当前源码、测试断言、[features.json](../../features.json)、[HANDOFF](../../HANDOFF.md) 和各 Feature Contract。HEAD=b16c926，存在大量已有未提交改动，提交号不能单独标识当前实现。本文是带日期的审查快照，不替代 Contract、BL/DC 或 Makefile。

## 1. 整体规划与当前被测范围

长期目标仍是多租户 CRM 与供应链处置：事实/知识 → 受控调查 → 提案/人工授权 → 写入/投递 → 追溯与评测。当前先验证其中的商机只读问答，不取消其余目标。

当前链路为：

```text
浏览器 → Next BFF → Python API（认证/组织/对象权限）→ SQL Mission/Run/Job
                                                           ↓
                                                       Worker claim
                                                           ↓
                       客户事实/政策 + 条件供应/报价 → 商机诊断
                                  ↓
                       Harness → Pi → 私有 Gateway → 模型
                                  ↓
                     Python Registry/Runtime/Scheduler → 领域读取
                                  ↓
                    Observation/Artifact/工具台账/Trace/模型交换
                                  ↓
                       当前权限/来源重验 → API → 页面轮询
```

- 范围：F45 商机选择、提问、调查、中文答案/引用/未知/冲突；F46 跟进和全部绑定料号供应；F47 固定 BOM 与报价时效，按配置启用。
- 不要求先完成：自动 routing、完整 Eval 平台、通用 checkpoint hooks、动态 Planner、完整 CRM/审批/渠道、实时 SSE 和完整应用 Compose。
- F35/F36=done；F48/F51/F52 等为 awaiting_acceptance。最新 F48 M1–M3 已有本地合成实现记录，旧“未开始”续接不能当最新状态。
- F36 opportunity_next_action 与 F45 情报调查是不同业务路径。F36 review 当前为确定性引用审核；F45 明确 needs_review，两者都不等于模型语义质量已经验证。

## 2. 当前 Feature 测试覆盖及问题

下表“覆盖”来自测试代码与 Contract，不表示本轮重新执行。各专项有重叠，不能将测试数量相加当作覆盖率。

| Feature / 当前状态 | 已有测试覆盖 | 问题与未验证边界 |
|---|---|---|
| F30 awaiting_acceptance | 清理非空保护、事务回滚、退役幂等、产品/硬件保留；记录有备份实际恢复验证 | 仅批准清理范围，不证明新业务能力或全量集成绿色 |
| F31 awaiting_acceptance | API身份、组织选择、受限数据库角色、同事务RLS、错误格式；Contract有历史真实Supabase认证记录 | 当前浏览器/Cookie/BFF/独立服务全链需重验；本轮未重跑真实认证 |
| F32 deferred，已有只读切片 | CRM版本化事实、最小引用、组织/对象scope隔离；有SQL基础测试 | 通用CRUD、编辑、Kanban及完整Proposal产品路径未完成 |
| F33 deferred，已有F45页面 | boundary/lint/TS/编译、F45固定响应浏览器QA、轮询/证据展示；F40有限历史SSE | 页面固定Replay；生产build最终失败；实时SSE/通用时间线/配额/审批未完成 |
| F34 awaiting_acceptance | 签名能力、模型/凭证覆盖、Schema、预算预留结算、未知usage、cached计费、health/ready与拒绝脱敏 | 真实OpenAI付费/BYOK未验收；无自动routing；Run能力不保证随活动Job失租即时撤销 |
| F35 done，运行补充待验收 | 幂等、并发唯一领取、lease/generation、有限重试、CLI报告、SIGTERM/SIGKILL回收、Trace验链 | 新turn/代次恢复，非自动精细续跑；按原因分流策略不足；生产调度/孤儿进程治理未验收 |
| F36 done，本地范围 | 多分支、确定性引用review、父汇总、失败阻草稿、知识ACL/版本/解析、旧记忆确认/撤销 | 无真实多角色语义质量证据；动态协作/自动分支复用未实现 |
| F37 contract_draft | 无完整产品验收路径 | 渠道、发送/回执、人工交接及审批联动未实现，当前联调排除 |
| F38 contract_draft | 各模块测试、Trace及fixture可复用 | 无统一场景版本/运行评分/归因/完整回放分叉与部署验收 |
| F39 awaiting_acceptance | 日期事件排序、Decimal、unknown、防在途重复计算、受限事实API、真实PG跨组织读写拒绝 | F43未定义的在途关系仍返回unknown；真实ERP/供应商未测 |
| F40 awaiting_acceptance | 哈希链/只追加、跨Case拒绝、并发Artifact版本、Last-Event-ID优先级、外Run/组织游标拒绝 | SSE只做有限历史回放；无实时订阅、完整dataset replay/fork或对象存储一致性证明 |
| F41 not_started，保留草稿 | 有未闭合目标测试与0023草稿 | /v1/proposals尚未实现；历史全量集成404/403差异，不算当前只读入口能力 |
| F42 awaiting_acceptance | 锁定真实Pi、工具往返、取消/超时/退出、敏感字段/乱序拒绝、第二引擎替换与版本一致 | 真实Pi不等于真实Provider；无Pi内存/推理栈恢复 |
| F43 contract_draft | F39已有unknown和防重复计算断言 | 在途来源权威/对账模型未实施；评测不得要求模型凭空给精确值 |
| F44 awaiting_acceptance | Python角色/capability矩阵、拒绝时Repository零调用、实际PG buyer写入 | 数据库提供组织/成员RLS，不是完整角色/对象级授权平台 |
| F45 awaiting_acceptance | 私有提问、幂等、并行前序/诊断、attempt/observation、重试/旧lease、撤权隐藏、当前成员角色校验 | 固定只读模板、非连续追问；一次claim异常原因未明；UI固定Replay |
| F46 awaiting_acceptance | 跟进时序、未知截止、时区/as_of、全Case料号、多轮调查、范围漂移、时间幂等 | 种子推进不是用户编辑/审批；as_of不重建历史库；漏斗/其他完整入口未交付 |
| F47 awaiting_acceptance | BOM/报价不可变版本、精确用量、期限/撤回/替代、改绑、ACL、报价角色及旧路径兼容 | 固定数据/Replay；用户录入、真实报价、全组织库存分配/采购执行未测 |
| F48 awaiting_acceptance，M1–M3 | 实际Mem0/Qdrant/SQLite、合成HTTP、跨会话Pi、来源/否定/时间、私有隔离、乱序/未知发布、计量、纠错遗忘、关系/旧数据桥、Dream与SQL+文件恢复 | 默认关闭/仅合成；无实际旧数据切换/真实服务验收；Dream是原文块无损加工；backup_pending保留；一次reconciliation_required原因未明 |
| F49 awaiting_acceptance | 实际Pi+本地Responses报文、五角色、调用ID/私有exchange/引用、超大批次不二次请求、RLS | 未跑真实OpenAI付费多角色链；固定报文不证明回答质量 |
| F50 awaiting_acceptance | Chat SSE/工具/usage、固定免费模型政策、Responses兼容回归；Contract有真实合成冒烟2请求/1工具 | 历史单角色冒烟不是完整多角色业务或浏览器全链，本轮未真实调用 |
| F51 awaiting_acceptance | 同源Schema/alias/Binding、参数反馈、原子调用台账、连续兼容组/屏障、串行消融、双进程容量、续租隔离/显式释放、取消/超限 | 七工具没有真实供应商资源声明；不是供应商QPS限流或外部副作用恢复 |
| F52 awaiting_acceptance，C1–C3 | 配对/水位、事务与进程死亡回滚、显式恢复、v3真实Pi双协议、快照一次接受、epoch/分块/压缩/否定/渐进读/超限 | 默认关闭；不保证无限窗口；默认非自动业务续接；一次finish等待5秒超时未定位 |

源码测试：[tests](../../tests)、[SQL测试](../../supabase/tests)。命令权威：[Makefile](../../Makefile)。细项与历史证据按对应 [contracts](../../contracts) 查阅。

### 执行记录的可信边界

| 来源 | 结果 | 使用边界 |
|---|---|---|
| 同一聊天上一轮实际执行，F48后续完成前 | make check：209 passed,63 deselected；f51集成：11 passed；f52集成：13 passed | 当时工作树通过，不作为最新F48 M2/M3的本轮重验 |
| 同一聊天上一轮实际执行web-check | boundary/lint/TS/编译完成；最终SecurityFatalError，Make退出2 | 整体失败，未升级或重试绕过，不声明已修复 |
| 最新F48 Contract/HANDOFF，本轮读取、未重跑 | f48-check：20 passed；f48集成：38 passed,1 deselected；make check：210 passed,72 deselected；Context独立回归：13 passed | 记录为本地合成/克隆/实际SDK证据，不冒充本轮执行或生产验收 |
| F31/F50历史真实调用记录 | 真实Auth；GLM两请求/一工具 | 仅历史限定范围，不代替当前完整入口验证 |
| 最新全量集成 | 本轮未运行 | 历史F41路由失败仍需单列；不能宣称全量绿色 |

待保留诊断线索：F45一次claim异常、F52一次finish超时、F48一次普通发布reconciliation_required，后续通过更多安全诊断定位；未复现不代表已修复。Qdrant本地索引提示、Starlette弃用提示不自动算产品失败，也不作为生产性能证明。

## 3. 用户自行联调前的必要条件

### A. 固定范围、版本与测试环境

- [ ] 选择一个固定工作树快照，记录代码版本、模型/政策/提示/工具配置版本、合成数据版本和as_of；不要边开发边作正式评分。
- [ ] 明确此次只测商机只读调查：答案、引用、未知、冲突与权限；审批/发送、连续追问、通用CRM不算预期能力。
- [ ] 使用独立测试库；API、Worker、Gateway指向同一目标。上一轮只读检查的业务库缺intelligence_request/model_exchange/tool_execution/Context/Memory表，本轮未重新查库。
- [ ] 准备可重复的组织、真实测试用户/成员角色、Worker身份和对象scope；准备商机、Case/全部料号、库存/需求/receipt、BOM/报价、知识及ACL。来源标synthetic，日期与版本固定。
- [ ] 数据装配目前主要在集成fixture，不能假设已有面向用户的通用种子导入命令。可以参考business_nodes/commercial集成种子，保持其隔离库限制，不直接对业务库调用。

### B. 按所选模式确认迁移依赖

下表是核对清单，不是迁移执行脚本。基础租户/CRM/供应/Gateway/Runtime/Trace/知识schema须先就绪；克隆现有基线与空库安装是两种不同情况。

| 模式 | 基础就绪后所需追加 | 当前限制 |
|---|---|---|
| F45–F47问答 + Gateway + F51 v2 | 0024问答、0025业务节点、0026 BOM/报价、0027模型交换、0028 GLM政策、0030工具台账、0031共享容量，按实际启用路径核对 | 0023审批草稿不是只读链前置；不要按编号无差别全部执行 |
| Context v3 | 追加0032会话/消息/工作状态、0033快照/epoch、0034压缩 | context-policy.json默认enabled=false；确认Python/sidecar协议与配置一致 |
| 合成Memory/Dream附加测试 | 再追加0035–0041，并准备锁定SDK、本地存储与回环摘要/embedding fixture | memory-policy.json默认enabled=false、syntheticOnly=true、dreamEnabled=false；不能直接接真实客户数据/云端服务 |

- [ ] 对每个追加确认目标库、受限角色、RLS、函数/列、重复执行与备份/回退边界；不把克隆通过当作业务库已迁移。
- [ ] 不为本轮联调执行清理迁移或改变旧Memory实际读取路由。

### C. 认证、进程和模型接线

- [ ] 先确认Web会话 → BFF → Python token重验 → 组织/对象scope。自动化测试里的Auth fixture不覆盖浏览器真实登录。
- [ ] 依照 [运行说明](../design/model-gateway-worker-contract.md) 核对API、Gateway和Worker环境；Gateway/Worker共享签名配置，凭证只在Python服务端。
- [ ] API能入队，Worker确实消费；/health只证明存活，Gateway /ready只证明其受限数据库就绪，不证明某组织模型权限或Provider可用。
- [ ] 当前页面 [page.tsx](../../frontend/src/app/page.tsx) 提交mode=replay。真实模型路径需要gateway模式及有效model_policy_id的入口接线；后端开关本身不足以让该页面调用真实模型。
- [ ] Replay与真实Gateway政策、Worker运行模式匹配；启用F46/F47的开关与数据齐全；未启用时不要期待报价角色或显式as_of路径。
- [ ] 模型窗口/output限制、协议、Schema与政策hash一致。先用固定模型建立基线，无须先实现自动routing。
- [ ] 解决web-check最终SecurityFatalError后才将生产构建门禁记为通过。该失败不等同“页面完全不能在开发模式打开”，但不能隐藏或绕过。
- [ ] 真实模型用合成案例、小规模调用并明确授权/预算；当前Memory真实服务准入单独处理，不以GLM接通推定Memory已可生产使用。

### D. 正常流程的观察点和完成标准

- [ ] 浏览器选组织/商机、提交后可看到queued/running或实际对应状态，再到completed/failed；刷新仍从持久状态读取。
- [ ] 同键同问题幂等；不同问题/目标/分析时间冲突可辨，不重复建调查。
- [ ] Job、Run、角色invocation、工具call/batch、Observation/Artifact、模型exchange可关联；启用Context时每请求快照也可关联。
- [ ] 完成不仅是HTTP200或Job completed：数量、时效、版本、引用、未知/冲突都符合固定输入；有效引用不自动算语义正确。
- [ ] 跨组织/同组织越权、用户撤权、来源撤回/BOM改绑后的请求或旧结果受限；Worker身份不扩大原用户权限。
- [ ] 当前UI用轮询即可；实时SSE不是正常问答联调前置。Memory/Dream先关闭跑主链，再单独增加合成附加组。
- [ ] 保存失败、未跑、跳过及未知usage，不只保存成功截图；不要把fixture模型报文与真实Provider结果混为一份证据。

## 4. 正式入口评测之前还需准备什么

完整Eval平台不阻塞手动联调，但在给质量分数之前至少准备：

1. **固定案例与oracle**：case_id、数据/政策/提示/工具版本、as_of、身份scope、问题、期望事实、必须保留的未知/条件及禁止行为。
2. **确定性与语义分开评分**：数值/报价分类/引用身份/权限用代码或明确清单检查；解释充分性、遗漏、历史与当前混淆用事先确定的人工量表。Judge后置。
3. **结果可归因**：逐案例保存配置、最终业务结果及运行/证据关联；费用/延迟包含失败重试，缺usage记未知，不当零。
4. **区分实验组**：默认v2、Context v3、合成Memory/Dream分别报告；Replay只证明接线；真实模型语义分数单独报告。
5. **评测范围独立**：先在F38 Contract切出当前只读入口的评测单元，不让未完成审批/渠道/完整Workbench成为假前置，也不静默改依赖。

推荐首批覆盖：普通无Case商机；旧逾期任务+后续跟进；固定BOM全料号需求；报价有效/到期/未知/撤回；无法对账的聚合在途保持unknown；跨组织/同组织越权；BOM改绑/证据撤回；重复提交/非法工具参数；输入/结果超限；依赖历史但Memory关闭或不可用。每类先有明确预期，再决定是否拆成正负例。

## 5. 机制建设的取舍与后续顺序

| 机制 | 是否必要 | 与本次联调的关系 |
|---|---|---|
| 轻量routing | 条件性保留：第二个授权模型和质量/费用/窗口差异得到证明后，先确定性选择policy | 不前置；已有显式政策/协议选择，先建立单模型基线 |
| Eval | 必要：先当前入口的版本化案例/runner/checker/人工量表，再故障/消融 | 手动联调可先做；正式质量评测需先有oracle和归因 |
| 中断恢复 | 必要增强：复用已有lease/reap/retry、取消、Working State/显式恢复，补恢复单位、原因分流、能力撤销与故障证据 | 不阻塞受控正常流程；持续真实模型运行/副作用前需进一步验收 |
| checkpoint hooks | 固定生命周期接点有用，通用插件框架目前无必要 | 先定义完整阶段/提交边界和依赖，再复用现有commit/checkpoint接口；不恢复Pi内存 |
| 错误映射 | 对面试和排障有价值，先整理现有行为，再用故障证据补策略 | 第6节交付总览，不作为联调阻塞，不改现有错误行为 |

后续事项已写入 [todo.md](../../todo.md)。这些是后续待办，不表示本轮已完成代码接线、迁移、故障修复或端到端测试。

## 6. 面试用错误处理与恢复总览

下面是现有主要边界的源码总览，不是穷举所有异常或证明每种组合都已测试。局部错误码、turn控制码、Job状态、HTTP响应与CLI退出码是不同层次，不能混作一张统一枚举。表中的恢复行为描述当前实现；值得补的策略另列在todo。

| 发生点 | 当前归类/传播 | 持久结果与恢复责任 |
|---|---|---|
| API认证/组织/capability/输入 | 401 unauthenticated；403 organization_forbidden/capability_forbidden；422 validation_error；数据库缺配置503 | 入队前拒绝；由调用者更正身份/输入/配置，不应启动模型重试。部分HTTPException仍使用detail，响应格式未全路径统一 |
| 问答幂等与开关/政策 | 同键冲突409 idempotency_conflict；开关/政策拒绝使用403/422/503 | 不创建重复调查；API begin路径将InvalidParameterValue归为冲突，不能仅凭公开码推断所有原始原因 |
| Gateway签名/政策/预算/Schema/Context准备 | 多种准备阶段异常统一403 model request denied；/ready依赖失败503 | SQL负责政策、预留/结算、私有exchange；公开403不足以区分权限、配置、数据库或预算根因 |
| Provider流中断/终态或usage缺失 | 流阶段报错，不能保证再修改HTTP状态；sidecar通常归为model_failed | usage未知不当零、保留可能计费的预留；Pi maxRetries=0，业务重试交给Worker，不能未知计费后盲目fallback |
| 工具未知/非法参数 | ToolOutcome(error,unknown_tool/invalid_arguments)回送模型 | 调用台账记录拒绝；模型可用新callId纠正，仍受批次/turn预算约束，不伪造成功 |
| 工具连接/超时 | ConnectionError/TimeoutError转service_unavailable反馈 | 账本记failed；当前turn可以收到错误反馈，不等于自动无限重试工具 |
| 工具撤权/失租/绑定/持久化/重复调用 | ToolAbort保留细码；Bridge大多提升为tool_failed，Trace另记细码 | 同事务接受实际结果、交付前再验；未持久化/失效结果不交给模型。Job层尚未充分利用细原因 |
| Scheduler截止/取消/容量 | deadline_exceeded映射timeout；cancelled走取消；准入等待重新检查门禁 | 失败取消相关执行；未知完成或失租容量隔离，需确认停止与合法token释放，不靠TTL自动释放 |
| Context配对/历史/预算/快照/epoch | ContextError细码记Trace；Bridge上抛HarnessTurnError(model_failed) | 完整消息单元/水位提交，快照一次消费，旧权限/来源/代次拒绝；确定性超限与临时模型错误上层仍较难区分 |
| Pi协议/进程 | 非法JSONL/版本/顺序/输出→protocol_failed；模型/工具/退出/超时有turn失败码 | Trace留失败记录并清理sidecar；恢复不从Pi内存或半个工具单元继续 |
| Specialist调查 | F45角色失败写attempt，前序任一异常使整轮tool_failed；F36记录specialist_failure并由汇总门禁裁决 | 角色结果和失败保留；当前主要重新调查，没有自动复用成功分支。引用校验不能替代语义评审 |
| Worker业务失败 | _fail调用SQL fail_runtime_job；cancelled终态，其余允许码未达次数则queued，达到次数failed | SQL同事务更新Job/Run与last_error、下次可见时间；当前不是按永久/临时原因决定重试 |
| Worker进程/数据库故障 | 捕获到的OSError/RuntimeError/PostgresError→process_error/worker_failed；启动异常→worker_start_failed | 不写completed；CLI脱敏报告。未完成租约由SQL到期回收、新generation领取，而非进程内自行恢复 |
| 正常停止/强杀/失租 | SIGTERM/SIGINT停止领取并取消turn，沿cancelled；SIGKILL无机会提交，后续reap | 正常取消不承诺自动续跑；强杀重领不等于已证明完整业务续跑。旧owner/代次不能提交，旧模型能力不保证即时撤销 |
| Memory召回 | disabled/not_requested/unavailable等结构化状态；scope不符memory_denied | 需要历史但不可用时明确缺失，不编造记忆；实际业务事实仍由领域服务判断 |
| Memory加工/发布 | candidate→review_pending；校验错误→quarantined；租约/存储/后端/未知写或usage→reconciliation_required | 复用持久工作项与generation；精确publication_key先对账，不能直接再次embedding；取消不接受迟到结果 |
| Memory删除/备份恢复 | 擦除失败erase_pending；未知备份backup_pending；恢复先blocked | 先deny/cancel，再清SQL/实际后端/派生；最新独立删除日志重放和requested清理完成后才开放，不声称全局擦除 |

主要源码：[API](../../src/product_api/app.py)、[Gateway](../../src/product_api/gateway.py)、[Harness](../../src/product_api/harness.py)、[Bridge](../../src/product_api/tool_bridge.py)、[Tool Runtime](../../src/product_api/tool_runtime.py)、[Scheduler](../../src/product_api/tool_scheduler.py)、[Worker](../../src/product_api/runtime.py)、[CLI](../../src/product_api/worker_main.py)、[SQL状态机](../../supabase/migrations/0019_f35_worker_lifecycle.sql)、[Context](../../src/product_api/context_store.py)、[Memory](../../src/product_api/memory_runtime.py)。对应测试族见第2节。

可以用工具撤权举例串起面试回答：原用户权限重验拒绝 → ToolAbort(permission_revoked) → Trace保存细码、turn归并tool_failed → Worker请求SQL失败转换 → Job重新排队或达到次数终止 → CLI outcome/退出码及页面持久状态。当前优势是授权/状态/证据边界清楚，缺口是细原因到恢复决策的映射尚粗，不能描述成已有完整统一恢复平台。

checkpoint也分层：pi_checkpoint保存事件序号/类型/代次/callId；工具台账保存已接受结果；F52保存完整消息与Working State/证据引用；Memory保存后台加工/对账与删除进度。前者是审计位置，不等于后者的业务恢复位置。默认Context会话按角色/generation新建，F52显式续接能力不等于F45重试已自动接好旧会话。
