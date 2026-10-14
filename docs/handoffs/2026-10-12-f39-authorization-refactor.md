# F39 授权重构交接（2026-10-12）

> 用途：下一会话恢复本次 F39 授权设计与未完成验证。它记录当前事实，不替代 `spec.md`、`features.json` 或 `contracts/F39.md`。

## 先读与边界

1. 读根目录 `AGENTS.md`、`HANDOFF.md`、本文、`contracts/F39.md`；然后只按 Contract 的引用定点读 `spec.md`。
2. 工作树是故意未提交的。不要 reset、checkout 或删除未跟踪文件；尤其不能混入 `.claude/settings.local.json`，也不能打印 ignored 的 `clash-fixed-*.yaml`。
3. 当前只处理 F39。未提交的 F40 Trace 草稿与 F35 Runtime command migration 是相邻但不在本次验收范围内的工作，必须保留、不能顺手扩展。
4. 不连接 ERP、供应商、采购执行器、真实模型或任何渠道；不改变 G1--G8。

## 本次已确认的产品与安全决定

这是求职向 demo，优先得到一个合理、简单且可解释的边界，**不在 F39 预先写死“采购员/硬件工程师分别能写哪些表”**。

当前 F39 的规则是：

```text
严格组织隔离。
当前组织的已验证成员可以人工录入供应链事实。
浏览器不能直连数据库。
Agent/Worker 不能直接修改供应链事实。
细角色/能力矩阵以后再设计。
```

实现分层：

```text
浏览器 → Next 薄 BFF → Python API → PostgreSQL
                              │             └─ RLS 最终检查 actor + organization + membership
                              └─ 同一事务设置 F31 request context
```

- `supplyagent_api` 是 Python API 使用的**技术数据库角色**，不是产品中的“采购员”等业务身份。
- 普通人工事实（Case、库存、需求、到货）采用 Python 的参数化 `INSERT`/`UPSERT`。写入不依赖 `SECURITY DEFINER`、函数 owner 或隐式绕过 RLS。
- RLS 只授予 `supplyagent_api` 当前组织、已验证成员的最小 `INSERT`/`UPDATE`；不授予 `DELETE`，浏览器也没有数据库凭证。
- 将来需要原子状态机的场景仍可用受控 SQL 函数：F35 claim/lease、F40 hash chain、F41 审批与不可逆外部动作。那不是 F39 的普通录入。
- Redis 可以做缓存、通知、协调或限流，不能成为 PostgreSQL 之外的权威事实或跨系统事务协调器。

## 已修改但尚未提交的 F39 内容

- 新增 `supabase/migrations/0015_f39_supply_fact_corrections.sql`：
  - 到货记录新增 `line_key`；唯一键改为 `(organization_id, receipt_reference, line_key)`，因此一张到货单可有多种料件。
  - 到货时间线索引改为只覆盖 `status = 'in_transit' AND expected_on IS NOT NULL`；已到货/取消记录仍保存为历史事实，只是不在该查询索引中。
  - 为四张 F39 事实表增加成员级 insert/update RLS policy，并给 `supplyagent_api` 最小 DML grant。
- `src/product_api/supply.py`：去掉四个 SQL command function 调用，改为连接到 F31 已建立 request context 后的参数化 SQL；将 PostgreSQL `42501` 转译为成员权限错误。
  - `inventory_position` 的 UPSERT 仅在新的 `verified_at >=` 已存观测时间时覆盖；API 强制请求提交非空 `verifiedAt`，避免旧观测把新事实覆盖。
  - `SupplyReceipt` 和插入逻辑使用 `line_key`。
- `src/product_api/app.py`：库存输入的 `verifiedAt` 必填；到货输入的 `lineKey` 必填；响应包含 `lineKey`。
- `supabase/tests/f39_supply.sql`：改为成员在 request context 下直接写入事实，不再调用 SQL command functions。
- `tests/test_product_api_supply.py` 与 `tests/test_product_api_supply_integration.py`：同步必填 `verifiedAt` 与 `lineKey`。
- `Makefile`：`supply-migrate` 追加 `0015`；增加 `supply-api-check`、`supply-api-integration-test`；离线 `test` 排除需要真实 Supabase 的 `supabase_live` 标记。
- `contracts/F39.md`、`features.json` 与根 `HANDOFF.md` 已改为反映“进行中、待真实数据库验证”。

## 已执行的验证

以下是本次重构后、在当前工作树实际成功的命令：

```text
ruff check src/product_api tests/test_product_api_supply.py tests/test_product_api_supply_integration.py
pytest -q tests/test_product_api.py tests/test_product_api_supply.py
git diff --check
```

结果：13 passed；pytest 显示 2 条已有 Starlette 弃用警告。Ruff 与 diff 检查通过。

续作后还实际成功运行了：

```text
make supply-api-check
make check
```

结果：前者 13 passed，后者 187 passed、18 deselected；两者均只有 2 条既有 Starlette 弃用警告。`git diff --check` 与 `features.json` JSON 解析也通过。

续作还完成了原先列出的三项本地代码/测试准备：`0015` 先删除新旧唯一约束以支持重复运行，删除旧 F39 `SECURITY DEFINER` 写函数，并在 `f39_supply.sql` 增加 Alpha 伪造 Bravo 组织写入必须由 RLS 拒绝的负面断言。到货 fixture 也已显式给出 `line_key`，与迁移中历史补齐后移除默认值的边界一致。

以下真实数据库验证**没有运行**，不能称作通过：

```text
make supply-migrate
make supply-test
make supply-api-integration-test
```

原因：Docker/Colima socket 当前不可用，最近的错误为：

```text
Cannot connect to the Docker daemon at unix:///Users/ruihan97/.colima/default/docker.sock
```

不要在没有用户同意的情况下自行启动服务。用户恢复后可自己运行 `make services-up`，或明确授权 Agent 启动；之后先执行上面三条 F39 命令。

## 下一会话的最小、安全顺序

1. `git status --short`，确认没有覆盖用户改动。
2. 用户启动本地服务或授权后，依次运行：

```text
make services-smoke
make supply-migrate
make supply-test
make supply-api-integration-test
make supply-api-check
make check
```

3. 只有这些实际结果通过后，更新 F39 Contract 的证据并把状态交给用户验收；不要在这之前 commit，也不要声称 F39 已完成。

## 工作树与提交

- 最近一个本地提交是 `ac926ac feat: establish v0.5 Python product boundary`。
- 本次 F39/F40/F35 草稿都在这个提交之后，当前未提交；没有 GitHub push。
- 当前可预期的改动包括：`Makefile`、`HANDOFF.md`、`contracts/F39.md`、`features.json`、`src/product_api/app.py`、`src/product_api/supply.py`、`src/product_api/trace.py`、F35/F39 migrations、F39 测试、Next supply BFF 路由；`.claude/settings.local.json` 属于用户本地设置，绝不能 stage。
