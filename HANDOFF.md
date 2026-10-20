# HANDOFF.md：SupplyAgent v0.6 交接

> 更新：2026-10-20
>
> 新会话先读本文件、`AGENTS.md`、`contracts/F42.md`，再按需读取 `spec.md` 对应节。Feature 清单和状态只看 `features.json`。

## 1. 当前产品决策

用户已明确要求：**完整保留参考“AI Native CRM 智能销售系统架构图”的全部功能目标，并删改原 SupplyAgent 的需求设计以融合实现。**

因此项目已从“单租户电子制造供应链 Harness”重基线为：

> **AI Native CRM 与供应链处置系统**

CRM/客户/商机/渠道/多租户是产品能力；现有采购异常处置、证据门控、审批令牌、回放和 Eval 是其中不可删除的供应链深业务域。权威设计在 `spec.md` v0.6。

2026-10-19 用户确认把 Agentic 开发重心固定为 **Python Product Harness + 可替换 AgentEnginePort + Pi Agent Core sidecar**。Pi 负责单个 turn 内的消息、模型和工具循环；Python/SQL 负责租户、上下文、策略、工具授权、模型网关、持久运行、副作用和审计。该规划不设一周期限，也不为赶时间删除远期模块；每次只开发一个可独立验证的 Feature。

## 2. 已完成的文档工作

- 重写 `spec.md`：逐项列出参考图的工作台、API/租户、Model Gateway/BYOK、Durable Runtime、Harness/Specialist、知识/RAG、MCP、人工决策/渠道、Trace/Eval/部署，并定义统一数据流和 G1 至 G8 边界。
- `features.json` 已更新为 v4：F01 至 F29 保持历史实现资产，F30 以后记录 v0.6 产品迁移与当前进度。
- `contracts/F30.md` 至 `contracts/F42.md` 已按各自状态记录目标、边界和证据，不再把已有 SQL、历史资产或 fixture 统一称为已实现。
- 采纳参考图的 Pi 执行能力，但不采纳其本地文件事实来源：F42 负责 `AgentEnginePort`、`PiRuntimeAdapter` 和 versioned JSONL；F35 的 SQL Worker 只通过端口驱动 turn，恢复事实仍在 SQL Trace/checkpoint。
- 架构图是 spec 的可视化投影：`diagram/supplyagent-v04/product-architecture.svg`。
- 新增 spec §3.1 迁移矩阵与 F39 至 F42 Contract 草案：它们分别覆盖供应链领域、产品 Trace、受控副作用、旧 Harness 到 Pi 的改造，避免旧 Feature 与新产品 Feature 混为同一完成状态。
- 2026-10-19 重写 Agentic Feature 边界：F42 拥有 Product Harness、`AgentEnginePort`、Pi JSONL 与 turn 语义；F34 拥有 Model Gateway；F35 拥有 SQL Job 生命周期；F36 拥有 Specialist 与知识；F41/F37 拥有副作用与渠道；F38 负责最终 Eval 和交付。
- F30 兼容审计已完成，唯一矩阵在 `docs/audits/F30-v04-compatibility.md`，待用户验收。

## 3. 现有代码与状态的诚实边界

- F01 至 F23 曾在单租户、模拟/固定样例边界内实现并有历史验证证据；**这不等于**多租户 CRM 产品已完成。
- F05 已冻结为 `eval_only`：其代码只能作为模拟、回归与 Eval 资产，不能接入 v0.6 的真实审批、API、Pi 或前端路径。
- 当前工作树在本轮文档改造前已经包含 F40 Trace 加固、Python Trace/Harness 适配、migration、测试、Makefile、`.claude/settings.local.json` 和若干 Skill 删除。它们没有被本轮回退或覆盖；提交前必须按所有者和 Feature 拆分。

## 4. 当前开发状态与下一步

用户已确认不一次性替换项目，而是采用：**Next.js/TypeScript 薄前端 → Python API/Worker → PostgreSQL → Python Product Harness → AgentEnginePort → 受控 Node Pi sidecar**。Next 只保留 React/SSR、浏览器会话承载与同源 BFF 转发；Python 是业务 API、身份/组织验证、领域服务、Gateway、Trace、Runtime、Worker 与 Harness 的唯一产品实现。Node 只允许作为无状态 Pi 上游 JSONL 适配器，不能有业务规则、数据库连接或持久凭证。

因此 F31 至 F42 的 Contract 已逐一加入该架构约束。2026-10-12 已删除须由 Python 重做的 Next/TypeScript 服务实现与对应测试；SQL migration、RLS 与表基础保留，但不能被误称为 Python 产品实现已经完成。

F36 已由用户确认 Contract 并进入 `in_progress`。已确定双层职责：Workbench 的 CRM 情报员、销售运营、客户沟通和 CRM 主管是 Mission 入口；后台以客户事实、商机诊断、业务政策为必需 Specialist，供应链风险和采购策略按真实业务条件启用。父 Agent 只汇总结构化 Claim/Citation/未知/冲突和 review，独占不可执行 `ProposalDraftArtifact` 的生成；F41 仍拥有真实 Proposal、审批和执行。

F36 P0 已实现 `src/product_api/specialists.py` 的角色、任务图、Claim/Citation、组织与 Mission scope、独立 Review、Mission 质量门禁和父 Agent 汇总。P1 已实现 Markdown/TXT/PDF 本地解析、组织公开/指定角色 ACL、官方/synthetic 来源、不可变版本与 Chunk、全文检索、Citation 校验、新版本自动过期旧引用、版本撤销和整篇撤回。公开来源目录在 `docs/research/F36-public-knowledge-sources.md`，三份演示文档在 `data/knowledge/demo/`。`make knowledge-migrate` 可重复执行，`make knowledge-test` 在真实 PostgreSQL/RLS 中通过。

F36 P2a 已实现 `SpecialistRunner`：Assignment 的工具集必须与实际 F42 注册表完全一致，工具必须是带显式参数校验器的 R0；Agent Engine 只能返回 16 KiB 内的结构化 output，组织、Mission、角色和 ID 由服务端重建，无效 Citation 会把 Claim 降级为 `rejected`。P2b 已让锁定版本的真实 Pi Agent 完成两次 Python 只读工具调用，将最终 JSON 文本通过通用 JSONL `output` 返回，再由 Python 校验为 `supply_risk` SpecialistResult。

F36 P2c 的第一个持久切片已接通：F35 Worker 先把两次 `supply.impact` 写为 F40 `tool_result` Artifact，Pi 返回的 Citation 由 Python 重新比对同 Trace/Case 的 Artifact ID、版本和哈希，然后写入 `specialist_result` Artifact，最后完成 SQL Job。`make runtime-integration-test` 在隔离克隆库验证了两个工具 Artifact、一个 Specialist Artifact 和 8 条 Event。`make f36-check` 为 30 passed，`make f42-check` 为 14 passed，`make runtime-check` 为 2 passed。该证据仍是无外部 Provider 的确定性 Replay；多 Specialist、独立 review、部分/失败结果汇合和父 Agent 汇总尚未实现。F32 Python CRM 服务仍是后续真实客户事实接线的明确缺口。

2026-10-20 用户确认以明确标记的模拟企业/客户/商机/政策完成 F36 portfolio demo；现有
HackRF One 图纸、MANIFEST 和派生 BOM 是产品来源，首批关键料为 Skyworks
`SKY13453-385LF`。官方资料只能保存原创摘要与来源链接；分销商 API 仅供单料号、单次
复核，不能批量采集或把凭证/完整响应落库。`data/knowledge/demo/` 已提供首批两份官方
摘要和两份 synthetic 文档。F36 P3 已实现 F32 的受限只读 `PostgresCrmFactRepository`：
只在 F31 同一 RLS/scope 事务内按机会 ID 返回最小 CRM 事实和版本化引用，不返回联系人
邮箱，不实现 CRM 写入或 HTTP 路由。`make f36-check` 为 32 passed；启动 Colima 后的
`make crm-facts-integration-test` 为 1 passed，验证正常读取、跨组织拒绝和同组织
scope 拒绝。下一单元是把该事实快照作为客户事实 Specialist 的受控 R0 工具接入
F35/F42 Mission，而不是扩大 F32。

F31 的 Python/BFF 实现与真实 Auth 验证已完成，状态为等待用户验收：`src/product_api/` 提供可注入 `AuthVerifier`、RBAC、受限 `TenantRepository`、`/health`、`/v1/session` 与组织选择入口；Next API Route Handler 已全部改为只转发 Python，边界检查会拒绝它们导入数据库或领域实现。默认未配置 Supabase 时安全拒绝业务请求；本地 fixture 已验证 repository 在 `supplyagent_api` 角色和 `app.set_request_context` 同一事务内工作。2026-10-12 经用户授权的 `make api-live-auth-test` 已以真实 Supabase 测试用户验证 access token 登录、Python `/user` 重验、组织选择与跨组织拒绝；测试凭证只在本地 `.env`。F32 的 Python CRM 服务尚未实现，相关 BFF 入口不能当作已可用业务 API。

F40 的 Python Trace Repository 已由 F42 和 F35 的真实集成测试使用；`0016_f40_trace_hardening.sql` 收紧 `SECURITY DEFINER` 函数 ACL、Run 游标、Case 一致性、Artifact 并发版本与链尾验证。`0017_f40_artifact_read_and_hash.sql` 将 Artifact 指纹改为 SQL 权威生成，并补 RLS 读取路径；不含对象存储一致性校验。F40 全回放尚未实现。F34 已补 SQL 预算预留/计量；F35 已补 Python Runtime/Worker 并等待验收。

F39/F44 的**授权设计重构**与其最小业务动作门禁已经完成运行验证，详细状态见 `docs/handoffs/2026-10-14-f39-f44-to-f42-session-handoff.md` 与当前 Contract。普通人工供应事实由 Python 在 F31 同一组织事务内作参数化 DML，PostgreSQL RLS 以已验证成员和当前组织作最终隔离；F44 要求 `supply:propose` 才能录入四类人工事实。Case scope 只接受受限的扁平业务上下文。2026-10-14 已实际通过 `make services-smoke`、`make supply-migrate`、`make supply-test` 与 `make supply-api-integration-test`；它们仍等待用户验收，且不代表真实 ERP、供应商或采购执行器集成。

已新增 F43 Contract 草案，记录持续补货料件的在途聚合与逐笔到货明细可能导致缺料投影长期 unknown 的数据语义问题。用户明确要求先完成 F42 Harness，再由用户选择单一事实来源、互斥语义或显式对账方案；在此之前不得修改 F39 计算、schema 或 API 来猜测其关系。

2026-10-14 用户确认先实施 F44 方案二：F39 的四类人工事实写入口必须在同一 F31 RLS 事务中读取成员角色并要求 `supply:propose`；组织 RLS 仍是跨组织隔离最终边界。F44 不新增数据库 schema、RLS policy 或前端授权逻辑，仓库/业务线 scope 与 Case 协作关系待真实业务需要后再设计。

F42 P0 受控协议基线已经验证：Python `Harness`/`PiRuntimeAdapter` 用 v1 JSONL 驱动真实 Node fixture sidecar，工具只允许 `supply.impact`，事件与 checkpoint 写入 F40 Trace。2026-10-19 已完成 P1：Node sidecar 锁定 `@mariozechner/pi-agent-core` 0.73.1，以无外部 Provider 的确定性 Replay 驱动真实 Pi `Agent` tool loop；Pi 的 `tool_execution_start` 映射为 JSONL `tool.call`，Python 执行并回传唯一允许的 `supply.impact`。`make f42-check` 为 6 passed，新增断言 `pi-agent-core@0.73.1` 标记，因此旧 fixture 不能单独通过；`make f42-integration-test` 为 1 passed，在本地 PostgreSQL/RLS 事务中验证 F40 Trace/checkpoint 和哈希链。此证据不代表真实 Provider/BYOK、业务写操作、SQL Worker、渠道或完整 F40 产品服务已接通。

F42 P0 至 P3 已完成并等待用户验收。P0 是 fixture 协议基线；P1 用锁定版本的真实 Pi Agent Core 和确定性 Replay 模型完成只读工具 turn；P2 已补两轮只读工具、流式增量的脱敏 Trace、取消、超时、模型/工具失败和 sidecar 退出分类；P3 以第二个不导入 Pi/Node 的最小 JSONL 引擎验证端口可替换，并校验 Pi package 与 lockfile 版本一致。`make f42-check` 为 13 passed，`make f42-integration-test` 为 1 passed。F42 本身不承担 Gateway、Worker、Specialist、副作用或 Workbench；其中 Gateway 与 Worker 的本轮状态见下段。

2026-10-19 本轮依序完成 F34 与 F35 核心实现：F34 采用用户选择的单 Run 短期 HTTP 能力，Python Gateway 持有策略、凭证、预算和计量；F35 用每组织专用 `maintainer` + `runtimeWorker: true` 成员身份领取 SQL Job，Python Worker 通过 F42 `AgentEnginePort` 驱动 Pi，并逐事件提交 F40 Trace。`make gateway-check` 4 passed、`make gateway-test` 通过、`make gateway-integration-test` 1 passed；`make runtime-check` 2 passed、`make runtime-test` 通过、`make runtime-integration-test` 1 passed。后者在隔离克隆库验证 HTTP 幂等入队→真实 Pi Replay→当前事实工具→持久 Trace/验链→Job 完成，并验证旧格式 Job 安全失败。未做真实付费 Provider 调用；真实部署的进程重启/cron 运维未演练。`make check` 本轮编译/lint 通过，离线测试为 23 failed、187 passed、21 deselected：22 项由既有 Skill 文件删除导致，1 项因沙箱禁止回环端口；该 F34 项在获准本机环境的 `make gateway-check` 中通过。提交门禁仍未全绿，未提交。

F42、F34、F35 完成当前实现后，F36 正按 P0 责任边界 → P1 组织知识/citation → P2 Specialist Runtime → P3 CRM/供应领域接线推进；之后才是 F41 Proposal/Execution、F37 渠道、F33 Workbench/SSE，最后由 F38 完成故障注入、回放、Eval 和 Compose 交付。每个 Feature 仍须先确认 Contract，再进入 `in_progress`。

不能跳过 F30/F31 直接做页面、渠道或真实模型调用：那会把单租户模拟 Harness 错接成带数据隔离和凭证风险的产品。

## 5. 本轮验证

- 2026-10-20：`make f36-check` 通过，结果为 7 passed；覆盖 Citation 门禁、必需/条件 Specialist、任务图阶段、跨组织/跨 Mission 拒绝、独立 review、partial/needs_review/blocked 和不可执行 ProposalDraft。此证据仅代表 F36 P0，不代表 P1 Wiki/RAG、真实 Pi Specialist turn、数据库/RLS 或 F32/F41 接线。
- 2026-10-20：F36 P1 加入后，`make f36-check` 为 19 passed；`make knowledge-migrate` 连续执行两次通过，`make knowledge-test` 在真实 PostgreSQL 中通过角色 ACL、跨组织拒绝、全文检索、哈希校验、新版本过期、撤销/撤回和内容不可变测试。此证据不代表 P2 Pi Specialist turn、F32 CRM 或 F41 Proposal 已接通。
- 2026-10-20：F36 P2a/P2b 加入后，`make f36-check` 为 30 passed、`make f42-check` 为 14 passed、`make runtime-check` 为 2 passed。覆盖精确工具集、R0/参数 schema、结构化 output、跨角色拒绝、服务端 scope/ID、Citation 失效降级，以及一个真实 Pi Replay `supply_risk` SpecialistResult。此证据不代表真实 Provider、三类必需 Specialist、F35/F40 Durable Mission 或父 Agent 汇总已接通。
- 2026-10-20：F36 P2c 第一切片的 `make runtime-integration-test` 为 1 passed；真实 PostgreSQL/RLS 验证 SQL Job → Pi Replay → F40 工具证据 → Citation 重验 → SpecialistResult → Job 完成。此证据只覆盖 `supply_risk` 分支，不代表多 Specialist 和父汇总已完成。
- 2026-10-20：经用户明确要求补齐缺失依赖，四个历史 `skills/*/SKILL.md` 已按 Git 基线原文恢复；恢复后 `make check` 为 241 passed、22 deselected，只有两条第三方库弃用警告。
- 2026-10-20：P2a/P2b 后全仓 `make check` 的 compile 和 lint 通过，测试为 22 failed、219 passed、22 deselected。失败仍全部由工作树中已删除的四个旧 `skills/*/SKILL.md` 引起；本轮未恢复、改写或跳过这些测试。
- 2026-10-20：加入 F36 P1 后，全仓 `make check` 的 compile 和 lint 通过，测试为 22 failed、207 passed、22 deselected。22 项失败均发生在既有测试加载已删除的 `skills/part_analyst/SKILL.md` 等四个 Skill 文件时；本轮未恢复、改写或绕过这些用户工作树删除。

- `features.json` 可被 `jq` 解析，且当前只有 F36 为 `in_progress`。
- 仓库当前没有 `check-punctuation.sh`；中文标点仅作人工 review，不得声称脚本门禁已执行。
- `git diff --check` 对本轮文档通过；全工作树仍有 `supabase/migrations/0004_f40_trace.sql` 的既有行尾空格。
- `make f42-check` 通过，结果为 6 passed。
- `make check` 未通过，结果为 22 failed、175 passed、20 deselected。失败都由工作树中已删除的 `skills/part_analyst/SKILL.md` 等 Skill 文件引发，首个异常是 `FileNotFoundError`。本轮没有恢复、改写或绕过这些删除。

## 6. 运行与授权

- 文档检查可用 JSON 解析和 Git diff；代码/迁移工作遵循 `make help` 中的唯一命令入口。
- `make check` 是提交前门禁；真实模型、真实渠道、真实分销商和批量外部调用都须另获 G5 授权。
- 不得把密钥、真实客户数据或渠道消息放入 fixture、日志、文档、截图或模型上下文。
