# F39 / F44 实测与 F42 开工交接（2026-10-14）

> 用途：新会话从当前未提交工作树安全恢复。本文是事实性交接，不替代 `AGENTS.md`、`HANDOFF.md`、`features.json` 或各 Feature Contract。

## 先读与工作边界

1. 读根 `AGENTS.md`、`HANDOFF.md`、本文、`contracts/F39.md`、`contracts/F44.md`；F39/F44 实测完成后，再读 `contracts/F42.md` 与其引用的 `spec.md` 小节。
2. 工作树是故意未提交的；不要 reset、checkout、删除未跟踪文件或混入 `.claude/settings.local.json`。
3. F40 Trace 草稿、F35 runtime command migration 是相邻但未验收的工作，必须保留，不可顺手修复或声称完成。
4. 不连接 ERP、供应商、采购执行器、真实模型或渠道；不改变 G1–G8。启动本地 Docker Compose 仅用于本地 PostgreSQL/Redis 验证，已获用户授权。

## 用户已确认的设计

### F39：人工供应事实

```text
浏览器 → Next 薄 BFF → Python API → PostgreSQL
                             │             └─ RLS 最终检查 actor + organization + membership
                             └─ 同一事务建立 F31 request context
```

- 普通人工事实（Case、库存、需求、到货）由 Python 参数化 DML 写入；不使用 F39 `SECURITY DEFINER` 写函数。
- `supplyagent_api` 是技术数据库角色，不是 buyer 等产品角色。
- RLS 对当前组织的已验证成员限制读写；不授予 DELETE；浏览器没有数据库凭证。
- Agent/Worker 不能直接修改这些事实。Harness 未来读取受控工具结果、产出提案；审批和不可逆副作用属于 F41。
- 到货 `line_key` 是必填行标识；库存 `verifiedAt` 必填，迟到快照不得覆盖较新观测。

### F44：已实施的最小业务动作门禁

- F39 四个写入口（Case、库存、需求、到货）都要求 `supply:propose`。
- `organization_admin`、`buyer`、`hardware_engineer` 有该能力；`planner` 等只读角色被拒绝。
- 检查在同一 F31 RLS 事务内读取 `organization_member.role`，然后由 Python `require_capability()` 判定；缺能力响应 `403 capability_forbidden`，不得调用 Repository。
- F44 不新增 schema、SQL migration、角色级 RLS 或前端权限逻辑。PostgreSQL RLS 仍是组织与成员隔离的最终边界；仓库/业务线 scope、Case owner/协作者关系以后再做。

### Case scope

`supply_case.scope` 是 Case 的业务上下文，不是 `organization_member.scope` 的权限范围。API 只接受扁平的：

```json
{
  "partNumbers": ["PART-A"],
  "locationCodes": ["MAIN"],
  "sourceReferences": ["work-order-9"],
  "tags": ["line-stop-risk"]
}
```

- 四个字段均为字符串数组；最多分别为 32、16、32、16 项。
- 元素最大长度分别为 160、80、160、40；禁止空值、重复值、未知字段和嵌套对象；编码总量最多 8 KiB。
- 这是 API 请求契约；数据库目前只检查 JSONB 顶层为 object。若将其提升为不可绕过的数据完整性规则，应先取得用户确认，再新建 migration 做 SQL 侧约束。

### F43：暂缓的在途一致性

`contracts/F43.md` 记录在途聚合与逐笔到货可能造成长期 unknown 的问题。用户决定先完成 F42 Harness 后再选择单一事实来源、互斥语义或显式对账方案；当前不得修改 F39 的缺料计算、schema 或 API 来猜测二者关系。

## 已修改的主要文件

- `supabase/migrations/0015_f39_supply_fact_corrections.sql`：line key、旧 F39 command function 删除、成员级 DML RLS policy、最小 grant。
- `supabase/tests/f39_supply.sql`：成员可写与跨组织伪造 insert 被 RLS 拒绝。
- `src/product_api/supply.py`：F39 参数化读写与确定性影响计算。
- `src/product_api/tenant.py`：`in_organization_as_member()` 在同一事务内读取成员关系。
- `src/product_api/rbac.py`：`CapabilityAccessError` 与 `require_capability()`。
- `src/product_api/app.py`：F39 HTTP 路由、F44 写门禁、Case scope schema。
- `tests/test_product_api_supply.py`：F39/F44 离线契约测试。
- `contracts/F39.md`、`contracts/F44.md`、`contracts/F43.md`、`features.json`、`spec.md`、`HANDOFF.md`：当前状态与后续边界。

## 已验证的事实

以下均在当前工作树实际成功：

```text
make supply-api-check
  → 17 passed，2 条既有 Starlette 弃用警告

make check
  → 191 passed，18 deselected，2 条既有 Starlette 弃用警告

git diff --check
features.json JSON 解析
```

以下尚未运行，不能宣称通过：

```text
make services-up
make services-smoke
make supply-migrate
make supply-test
make supply-api-integration-test
```

之前 Docker/Colima socket 不可用；本次用户已明确要求启动本地测试环境。不要把服务冒烟或 fixture 测试说成真实 ERP、供应商、模型或渠道集成。

## 新会话的安全顺序

1. `git status --short`，确认并保留已有脏工作树。
2. 启动本地环境并验证：

```text
make services-up
make services-smoke
make supply-migrate
make supply-test
make supply-api-integration-test
```

3. 若任一步失败，完整阅读错误，先判断是环境、迁移重复执行、RLS policy 还是 API 问题；同一问题不得盲目重复修复超过三次。通过后更新 F39/F44 Contract 的真实服务证据，但不要在用户验收前称 F39 完成。
4. 之后开始 F42：先读 `contracts/F42.md`，并按 Contract 指向最小范围阅读 `spec.md`。F42 目前只是草案，必须先形成用户确认的细化 Contract、明确固定 JSONL fixture/版本协议、Python Harness 与 Node Pi sidecar 的无状态边界、F40 Trace/F35 Worker 的未完成依赖，再改代码。
5. F42 不能把 Pi Coding Agent 的 terminal、filesystem、shell、数据库凭证、Provider 凭证或渠道能力暴露为产品工具；不能用 Mock/Replay 冒充真实 Pi、模型或渠道集成。

## 提交状态

- 最近本地提交：`ac926ac feat: establish v0.5 Python product boundary`。
- 当前改动均未提交、未 push。
- `.claude/settings.local.json` 是用户本地设置，绝不能 stage。
