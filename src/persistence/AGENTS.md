# Persistence 模块指引

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有 PostgreSQL 与 Redis 的存储 contract：对话 state、Run / ToolCall / PermissionDecision 审计记录、metrics_snapshot 落 PostgreSQL；缓存 / 幂等去重 / 分布式锁走 Redis。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Persistence 行、`DECISIONS.md` D05 / D11、`docs/spec/data-model.md` 对应表章节；不读取归档设计正文。

## 不变量与 contract

- 所有写入复用调用方事务；服务函数不得隐式 commit。失败时回滚该操作的 savepoint。
- 分配 plan.version 前必须持有该 demand 行 FOR UPDATE，锁保留到外层事务结束；禁止无锁 max(version)+1。
- 原始层不可写；静态导入只消费 normalized，发现内容冲突即回滚，不覆盖核验结论。
- 审计权限与两张可更新例外表以 `docs/spec/data-model.md` §6 为准；不得用应用角色执行迁移。

## 修改后验证

执行 `make compile`、`make lint`、`make test`，再执行 `make test-integration`。迁移变更还须 `make migrate`。测试使用独立 schema，验证事务回滚、读回、并发及清理；不得借用正式表清空数据。`tests/test_schema.py` 将迁移结果与契约 DDL 独立建出的目标结构对照。
