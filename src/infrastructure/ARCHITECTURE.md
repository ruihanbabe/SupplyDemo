# Infrastructure 模块

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块职责或代码落点变更时由 Codex 写入
> 独占：本模块在根 `ARCHITECTURE.md` 模块表中那一行的展开说明
> 不收录：不变量正文（见同目录 `AGENTS.md`）、跨模块依赖方向（见根 `ARCHITECTURE.md`）

`database.py` 提供 `database_url()`，从项目 `.env` 与进程环境读取 `SUPPLYAGENT_DATABASE_URL`（进程环境优先），返回 SQLAlchemy URL；迁移使用已锁定的 psycopg 同步驱动。配置缺失或格式错误明确失败，错误信息不包含连接凭据。

迁移 owner 与受限应用角色的职责见 `docs/spec/data-model.md` §6。应用运行入口、LLM、Redis 及供应商 Adapter 不在本次骨架范围内。
