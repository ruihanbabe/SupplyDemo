# SupplyAgent v0.4：Codex 会话交接

> 生成日期：2026-10-10（Asia/Shanghai）
> 用途：在新的 Codex 或 ChatGPT Work 对话中恢复当前项目，不要把本文件当作产品需求的权威来源。权威需求仍是 `spec.md`、`features.json` 与对应 `contracts/Fxx.md`。

## 1. 新对话先做什么

1. 阅读根目录 `AGENTS.md`、本文件与 `HANDOFF.md`。
2. 若要实现某个 Feature，先读其 `contracts/Fxx.md`，再按该 Contract 指向的 `spec.md` 节阅读；不要通读后凭印象修改。
3. 先运行 `git status --short`。工作树**有大量故意保留的未提交改动**；不得用 `git reset --hard`、`git checkout --` 或删除旧目录来“清理”。
4. 一次只推进一个 Feature。实现后把实际实现内容、验证命令和结果追加到该 Feature Contract 的台账，最后运行与影响范围相称的检查。

可直接把下面的话作为新对话的第一句：

```text
请先阅读 docs/handoffs/2026-10-10-codex-session-handoff.md、AGENTS.md、HANDOFF.md，确认当前工作树不应重置。然后按交接文档继续：先给出 F42 下一实现单元的最小计划与验收证据，确认后再改代码。不要接真实模型、渠道或付费 API。
```

## 2. 项目现在是什么

SupplyAgent 的主线不再只是 Python 缺料计算脚本，而是一个以“缺料异常调查与补充建议”为深度业务场景、同时保留 AI Native CRM 集成面的多租户 Agent 系统。

目标数据流如下：

```text
登录与组织
  → API
  → SQL / RLS
  → 领域服务
  → Job / Worker
  → Pi / Gateway / 工具
  → Trace
  → Proposal / 审批 / 渠道
```

用非技术语言解释：

| 环节 | 它负责什么 |
| --- | --- |
| 登录与组织 | 确认“谁在操作、属于哪家公司、能做什么”。 |
| API | 前端与后端之间的受控入口，负责校验请求，不能让浏览器直接越权读写数据库。 |
| SQL / RLS | PostgreSQL 是权威记录；RLS（行级权限）让数据库再确认一次“这个人只能看到本组织被允许的数据”。 |
| 领域服务 | 把采购/客户/商机等业务规则集中起来，避免在页面或模型提示词中随意拼规则。 |
| Job / Worker | 耗时任务先排队；Worker 在后台领取、续租、重试或回收任务，而不是让网页一直等待。 |
| Pi / Gateway / 工具 | Pi 是模型 Agent 的运行协议/执行器；Gateway 管模型供应商、用量和密钥引用；工具是 Agent 能安全调用的受控业务能力。 |
| Trace | 每一步留下可追溯证据：发生过什么、调用什么工具、得到什么结果；不能只相信模型最后一句话。 |
| Proposal / 审批 / 渠道 | Agent 只能提出建议；涉及写入、外部发送或副作用时，必须经人工审批，再由受控 Worker 执行。 |

架构图在 `diagram/supplyagent-v04/product-architecture.svg`。它是目标与当前实现边界的可视化，不代表图中所有外部集成均已真实接通。

## 3. 不可破坏的边界

- 真实模型、付费 API、分销商批量查询和真实渠道发送，均须用户明确授权（G5）。当前默认只能跑本地 stub/replay。
- 凭证、API key、服务角色密钥不得写进模型上下文、Trace、fixture、日志或 Contract（G1）。数据库只存 `credential_ref` 一类引用。
- Agent/LLM 不可直接写业务事实；写操作经 Proposal、人工审批和受控执行路径（G2）。
- 数量、价格、交期等确定性业务数值不可由 LLM 编造（G4）；未知值使用 `NULL`，不可用 `0` 假装已知。
- 不做排产（G8），不把 Redis 当权威业务数据；权威状态在 PostgreSQL。
- 数据库结构只通过迁移改；新增产品表在 `supabase/migrations/`，不可绕开迁移直接手改。
- 旧 Python F05 路径仅 `eval_only`：不能被新的浏览器、模型、Pi 或渠道链路调用。

## 4. 工作树与旧代码：为什么不能“整个重写”

当前工作树是有意未提交的。可粗略分成两类：

1. **必须保留但不能接入新产品主路径的 F05 历史实现**：
   `src/gate/evidence.py`、`src/knowledge_base/kb.py`、`src/orchestrator/assembly.py`、`src/runtime/segment.py`、`src/simenv/api.py`、`src/simenv/engine.py`、`config/humans.yaml`、`src/humans/`、`tests/test_humans.py`。
2. **v0.4 新产品实现**：`frontend/`、`supabase/`、`contracts/F30.md` 至 `contracts/F42.md`、`diagram/`、`docs/`，以及 `spec.md`、`features.json`、`Makefile`、`HANDOFF.md` 等文档/入口调整。

结论：不需要整个项目重写。新架构以 Next/Supabase/SQL Runtime 作为产品主路径；旧 Python 代码保留为历史评估资产，不能因为“不整齐”而删除或偷偷接回去。

## 5. 已落地的 Feature 与真实验证

状态以 `features.json` 和 Contract 为准；下面是本会话最后确认的摘要。

| Feature | 当前状态 | 已完成的可观察结果 | 尚未完成/限制 |
| --- | --- | --- | --- |
| F30 | awaiting_acceptance | v0.4 兼容性审计与新架构基线已建立。 | 等待人工验收。 |
| F31 | in_progress | 多租户/RLS/RBAC 的本地 SQL 基础、前端生产构建已验证。 | 真实 Supabase Auth URL、publishable key、测试用户和真实 HTTP 会话链路未验证。 |
| F32 | awaiting_acceptance | CRM 对象、范围、RLS、审计、review/proposal 状态机本地实现并验证。 | 接受 CRM proposal 不执行写入；真正审批执行属于 F41。 |
| F34 | awaiting_acceptance | Gateway 的 `model_policy`、`model_usage`、只读 Replay provider、本地安全边界已验证。 | 未接任何真实 Provider/BYOK；需 G5 授权后才可做。 |
| F35 | awaiting_acceptance | SQL durable runtime：mission/run/job、幂等入队、claim/lease/heartbeat/reap/cancel，以及可注入 Worker skeleton 已验证。 | Worker 尚不启动真实 Pi；Pi 生命周期接到 F42。 |
| F39 | in_progress | 缺料/供给业务产品化表与时间线已落地，本地验证通过。 | 真实 Supabase HTTP 接入仍未验。 |
| F40 | in_progress | SQL Trace 的 event/artifact/run、哈希链、RLS 和 repository 基础已落地。 | Event-ID SSE、计量、replay、Pi 的真实持久化与 checkpoint 仍未完成。 |
| F42 | in_progress | Pi JSONL 协议解析、版本/事件白名单、敏感字段拒绝、Pi 事件到 Trace intent 的映射已完成并测试。 | 受控子进程、取消、将真实 Pi 事件写入 F40 Trace/checkpoint、SQL 恢复尚未完成。 |

历史采购核心 F01–F07 不应被本次 v0.4 工作回退：其中 F01/F02 已完成；F03、F06–F23 多数仍是历史 `awaiting_acceptance`，F05 是 `deferred/eval_only`。F24–F29 未开始。F33 当前为 `in_progress`，F36–F38 与 F41 仍为 Contract draft；实施顺序须再次以依赖与用户确认确定。

## 6. 已有文件地图

```text
spec.md                                  总体需求与全局边界（SSOT）
features.json                            Feature 状态（SSOT）
contracts/F30.md … F42.md               每个 Feature 的范围、台账与验收证据
HANDOFF.md                               较短的长期项目状态
diagram/supplyagent-v04/product-architecture.svg
                                         v0.4 架构图
frontend/                                Next.js 产品入口、API、领域/运行时/Gateway/Pi/Trace 代码及测试
supabase/migrations/0001…0012_*.sql      多租户、供应链、Trace、CRM、Gateway、Runtime 的 schema/RLS
supabase/tests/                          对应迁移与 RLS/行为的 SQL 验证
Makefile                                 所有权威检查命令的入口
```

重要迁移分组：

- `0001_f31_tenant.sql`：组织、成员、角色与 RLS 基线。
- `0002`–`0003`：F39 缺料/供给及时间线。
- `0004_f40_trace.sql`：Trace。
- `0005`–`0008`、`0011`–`0012`：F32 CRM、命令、互动、人工 review、范围和审计。
- `0009_f34_gateway.sql`：模型策略与用量账本。
- `0010_f35_runtime.sql`：mission/run/job durable runtime。

## 7. 最可信的最新验证证据

在 2026-10-10，本会话实际执行并通过：

```text
make crm-migrate && make crm-test
make gateway-migrate && make gateway-test
make runtime-migrate && make runtime-test
make web-check
make check
jq empty features.json && git diff --check
```

已知结果：

- CRM 检查覆盖跨组织隔离、对象范围、审计、事实、过期和人工 review。
- Gateway 检查覆盖策略隔离与 `real_calls_enabled = false` 的默认禁止。
- Runtime 检查覆盖幂等入队、互斥领取、过期 lease 回收、取消；租约逻辑已使用 `clock_timestamp()`，避免 PostgreSQL 事务时间冻结导致错误判断。
- `make web-check`：17 个 frontend 测试、lint、边界检查与 Next production build 通过。
- `make check`：`174 passed, 15 deselected`，耗时约 105 秒。

这些证据仅说明本地代码/数据库测试通过；不等于真实 Supabase、真实模型、WhatsApp/飞书或外部供应商已接通。

## 8. 推荐的下一实现单元：F42

最连续、风险最小的下一步是完成 **F42 的下一小段**，不要同时接 Provider 或渠道：

1. 先阅读 `contracts/F42.md`、`contracts/F35.md`、`contracts/F40.md` 和相应 `spec.md` 节。
2. 设计最小的受控 Pi 子进程端口：明确 JSONL stdin/stdout 协议、允许的可执行路径、超时、取消、退出码与 stderr 的处理；不得把凭证传给子进程。
3. 将已验证的 Pi 事件真正写入 F40 的 Trace repository，并定义何时保存 checkpoint；保留现有 `toTraceIntent` 的协议层，不让协议解析器直接绕过 Trace 权限。
4. 为成功、未知事件、敏感字段、子进程非零退出、取消和中断后恢复各写窄测试。
5. 将实际实现、命令、结果和仍未实现项追加进 `contracts/F42.md`，随后运行 `make web-check` 和与 F40/F35 相关的迁移测试；影响扩大时再运行 `make check`。

替代路线只有在用户提供真实 Supabase 配置并明确要求时才做 F31 的真实 Auth/HTTP 集成。不要为了“看起来完整”而自行开启真实模型、BYOK、WhatsApp、飞书或分销商调用。

## 9. 人工 review 的建议顺序

如果新 session 的目标是学习和人工审阅，不要从所有代码开始：

1. 看本文件第 2 节的数据流，再看 `diagram/supplyagent-v04/product-architecture.svg`。
2. 看 `spec.md` 的全局边界（G1–G8），理解什么事情系统刻意不能做。
3. 看 `contracts/F31.md` 和 `0001_f31_tenant.sql`：先理解“谁能看到谁的数据”。
4. 看 `contracts/F39.md` 及供应链迁移：确认缺料调查的权威事实长什么样。
5. 看 `contracts/F35.md` 和 `0010_f35_runtime.sql`：理解后台任务的状态、领取与恢复。
6. 看 `contracts/F40.md`、`contracts/F42.md`：理解模型执行为何必须可追溯。
7. 最后看 F32 与未来 F41：理解为什么 Agent 建议和真正写入/外发必须分开。

每次人工审阅只问一个具体问题，例如：

```text
请用不假定我懂后端的方式，带我审阅 F35：从 runtime_job 的一条记录开始，解释它怎样被领取、续租、失败回收和取消；每一步对应哪个文件和测试？先不改代码。
```

## 10. 新会话不能误称的事实

- 不可说“图里的 CRM/渠道/模型集成已全部实现”。当前实现保留了架构接口和部分本地骨架，但真实外部集成尚未验收。
- 不可说“F31 已完成”，因为真实 Supabase Auth/session HTTP 未实测。
- 不可说“F42 已完成”，因为还没有真实受控 Pi 子进程到 SQL Trace/checkpoint 的闭环。
- 不可把 `make check` 通过说成真实服务端到端成功；它是离线门禁。
- 不可为加快开发而删除 F05 旧代码或降低 G1–G8、RLS、审批、Trace 约束。

## 11. 交接时的第一条决策

新对话首先应确认目标是以下哪一种，再开始修改：

- **继续实现**：按第 8 节从 F42 的最小受控 Pi→Trace 单元继续。
- **人工学习/review**：按第 9 节逐层审阅，不改代码。
- **接真实服务**：先由用户提供/配置环境并明确授权成本与外部副作用；从 F31 的真实 Supabase Auth/HTTP 开始。

在没有这条选择前，最安全的默认是只阅读、解释和规划，不接外部服务。
