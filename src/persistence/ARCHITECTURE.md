# Persistence 模块

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块职责或代码落点变更时由 Codex 写入
> 独占：本模块在根 `ARCHITECTURE.md` 模块表中那一行的展开说明
> 不收录：不变量正文（见同目录 `AGENTS.md`）、跨模块依赖方向（见根 `ARCHITECTURE.md`）

尚未成立（占位）。拥有 PostgreSQL（事务 / 审计只插入表）与 Redis（缓存 / 幂等去重 / 分布式锁）的存储 contract。把运行时状态转为可持久化 / 可恢复记录。实现与测试待建，边界以仓库根 `ARCHITECTURE.md` 的 Persistence 行为准。
