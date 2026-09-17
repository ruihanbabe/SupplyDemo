# Persistence 模块

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块职责或代码落点变更时由 Codex 写入
> 独占：本模块在根 `ARCHITECTURE.md` 模块表中那一行的展开说明
> 不收录：不变量正文（见同目录 `AGENTS.md`）、跨模块依赖方向（见根 `ARCHITECTURE.md`）

数据库结构的执行入口为根目录 `alembic/`：`env.py` 使用 Infrastructure 的连接配置，`versions/0001_initial.py` 与同名 SQL 固化初始迁移。目标结构以 `docs/spec/data-model.md` 为准；迁移运行时不解析 Markdown。

`tests/test_schema.py` 对照目标 DDL 检查迁移漂移，在独立事务与随机 schema 中验证升级、回退、权限及故障回滚。迁移保留集群级 `supplyagent_app` 角色，回退只移除本迁移的表，不删除可能被其他数据库引用的角色。

`import_normalized.py` 通过 `make import-data` 消费规范化层五个文件；五表导入处于同一事务，使用主键幂等并逐字段核对。冲突报错回滚，不覆盖既有身份状态，不把数量口径标为已核验。

`procurement.py` 的 `ProcurementRepository` 由调用方注入 SQLAlchemy connection，外层事务由调用方提交/回滚；每个服务操作使用 savepoint，异常不遗留部分数据。需求 ID 重用必须内容一致，已有展开不被静默覆盖。库存、占用、在途在同一个 SELECT 快照中读取；数值 JSON 显式转为文本再解码为 Decimal。

方案版本分配在 demand 行 `FOR UPDATE` 锁内进行，直至外层事务提交；方案头与行同事务写入。`read_plan` 支持从新连接读回冻结证据。Alembic `0002` 为用户确认的数量未知值修订，回退遇到已有 NULL 时拒绝执行，不转零也不丢行。
