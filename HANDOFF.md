# HANDOFF.md — SupplyAgent v0.5 交接

> 更新：2026-10-12
>
> 新会话先读本文件、`AGENTS.md`、`docs/handoffs/2026-10-12-f39-authorization-refactor.md`，再读当前 Feature 的 Contract。Feature 清单和状态只看 `features.json`。

## 1. 当前产品决策

用户已明确要求：**完整保留参考“AI Native CRM 智能销售系统架构图”的全部功能目标，并删改原 SupplyAgent 的需求设计以融合实现。**

因此项目已从“单租户电子制造供应链 Harness”重基线为：

> **AI Native CRM 与供应链处置系统**

CRM/客户/商机/渠道/多租户是产品能力；现有采购异常处置、证据门控、审批令牌、回放和 Eval 是其中不可删除的供应链深业务域。权威设计在 `spec.md` v0.5。

## 2. 已完成的文档工作

- 重写 `spec.md`：逐项列出参考图的工作台、API/租户、Model Gateway/BYOK、Durable Runtime、Harness/Specialist、知识/RAG、MCP、人工决策/渠道、Trace/Eval/部署，并定义统一数据流和 G1–G8 边界。
- 更新 `features.json` 为 v3：F01–F29 标为历史实现资产；新增 F30–F42 的 v0.5 产品与迁移 Feature。
- 新建 `contracts/F30.md` 至 `contracts/F38.md`：均为 `contract_draft`，尚未启动实现。
- 采纳 Folio 图的 Pi 执行拓扑，但不采纳其本地文件事实来源：F35 规定由 SQL Worker 控制 PiRuntimeAdapter Node 子进程，经 versioned JSONL IPC 写回 SQL Trace/checkpoint；F33 Workbench 调整到审批/渠道之后，避免前端优先。
- 架构图是 spec 的可视化投影：`diagram/supplyagent-v04/product-architecture.svg`。
- 新增 spec §3.1 迁移矩阵与 F39–F42 Contract 草案：它们分别覆盖供应链领域、产品 Trace、受控副作用、旧 Harness→Pi 的改造，避免旧 Feature 与新产品 Feature 混为同一完成状态。
- F30 兼容审计已完成，唯一矩阵在 `docs/audits/F30-v04-compatibility.md`，待用户验收。

## 3. 现有代码与状态的诚实边界

- F01–F23 曾在单租户、模拟/固定样例边界内实现并有历史验证证据；**这不等于**多租户 CRM 产品已完成。
- F05 已冻结为 `eval_only`：其未提交代码仅可作模拟、回归与 Eval 资产，不能接入 v0.5 的真实审批、API、Pi 或前端路径；本轮没有修改其业务代码。
- 当前工作树已有 F05 相关未提交改动：`src/gate/evidence.py`、`src/knowledge_base/kb.py`、`src/orchestrator/assembly.py`、`src/runtime/segment.py`、`src/simenv/api.py`、`src/simenv/engine.py`、`config/humans.yaml`、`src/humans/`、`tests/test_humans.py`，以及本轮文档改动。不得覆盖或混入。

## 4. 2026-10-10 架构重基线与下一步（一个 Feature）

用户已确认不一次性替换项目，而是采用：**Next.js/TypeScript 薄前端 → Python API/Worker → PostgreSQL → Python PiRuntimeAdapter + 受控 Node Pi sidecar**。Next 只保留 React/SSR、浏览器会话承载与同源 BFF 转发；Python 是业务 API、身份/组织验证、领域服务、Gateway、Trace、Runtime、Worker 与 Harness 的唯一产品实现。Node 只允许作为无状态 Pi 上游 JSONL 适配器，不能有业务规则、数据库连接或任何凭证。

因此 F31–F42 的 Contract 已逐一加入该架构约束。2026-10-12 已删除须由 Python 重做的 Next/TypeScript 服务实现与对应测试；SQL migration、RLS 与表基础保留，但不能被误称为 Python 产品实现已经完成。

下一实现单元原为 F31，现已完成本地与真实 Supabase Auth 验证。后续按 F39/F40/F32/F34/F35/F42 的当前 Contract 逐项在 Python 重建；每项自行定义固定输入与可复跑验收，不依赖已删除的 Next 服务实现。

F31 的 Python/BFF 实现与真实 Auth 验证已完成，状态为等待用户验收：`src/product_api/` 提供可注入 `AuthVerifier`、RBAC、受限 `TenantRepository`、`/health`、`/v1/session` 与组织选择入口；Next API Route Handler 已全部改为只转发 Python，边界检查会拒绝它们导入数据库或领域实现。默认未配置 Supabase 时安全拒绝业务请求；本地 fixture 已验证 repository 在 `supplyagent_api` 角色和 `app.set_request_context` 同一事务内工作。2026-10-12 经用户授权的 `make api-live-auth-test` 已以真实 Supabase 测试用户验证 access token 登录、Python `/user` 重验、组织选择与跨组织拒绝；测试凭证只在本地 `.env`。F32/F39 的 Python 上游尚未实现，相关 BFF 入口不能当作已可用业务 API。

F40 有未提交的 Python Trace Repository 与路由草稿，但尚无专门测试；不可称为完成或把它混入当前 F39 工作。F35 也有未提交的 SQL command migration 草稿；当前没有开始其 Python Runtime/Worker/Pi 实现。

当前正在推进 F39 的**授权设计重构**与其最小的 F44 业务动作门禁，详细状态见 `docs/handoffs/2026-10-12-f39-authorization-refactor.md`。此前“F39 已完成、以 `SECURITY DEFINER` SQL 命令函数写入、并已通过本地 RLS/API 集成验收”的说法只是一段已被推翻的历史证据，不能作为当前验收依据。当前决定是：普通人工供应事实由 Python 在 F31 同一组织事务内作参数化 DML，PostgreSQL RLS 以已验证成员和当前组织作最终隔离；F44 要求 `supply:propose` 才能录入四类人工事实。Case scope 只接受受限的扁平业务上下文。离线检查已为 `make supply-api-check` 17 passed、`make check` 191 passed；本地 Docker/数据库当前不可用，所以新的迁移和真实 RLS/端到端验证均尚未运行。

已新增 F43 Contract 草案，记录持续补货料件的在途聚合与逐笔到货明细可能导致缺料投影长期 unknown 的数据语义问题。用户明确要求先完成 F42 Harness，再由用户选择单一事实来源、互斥语义或显式对账方案；在此之前不得修改 F39 计算、schema 或 API 来猜测其关系。

2026-10-14 用户确认先实施 F44 方案二：F39 的四类人工事实写入口必须在同一 F31 RLS 事务中读取成员角色并要求 `supply:propose`；组织 RLS 仍是跨组织隔离最终边界。F44 不新增数据库 schema、RLS policy 或前端授权逻辑，仓库/业务线 scope 与 Case 协作关系待真实业务需要后再设计。

F32、F34、F35、F40 的 PostgreSQL/RLS 基础仍在；对应 Python 服务尚未实现。F42 回到 Contract 草案，等待 Python Harness/PiRuntimeAdapter 从头实现。真实 Provider/BYOK、真实 Pi 子进程和渠道仍未接通。

不能跳过 F30/F31 直接做页面、渠道或真实模型调用：那会把单租户模拟 Harness 错接成带数据隔离和凭证风险的产品。

## 5. 运行与授权

- 文档检查可用 JSON 解析和 Git diff；代码/迁移工作遵循 `make help` 中的唯一命令入口。
- `make check` 是提交前门禁；真实模型、真实渠道、真实分销商和批量外部调用都须另获 G5 授权。
- 不得把密钥、真实客户数据或渠道消息放入 fixture、日志、文档、截图或模型上下文。
