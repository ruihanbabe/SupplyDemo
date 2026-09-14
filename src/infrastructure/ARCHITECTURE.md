# Infrastructure 模块

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块职责或代码落点变更时由 Codex 写入
> 独占：本模块在根 `ARCHITECTURE.md` 模块表中那一行的展开说明
> 不收录：不变量正文（见同目录 `AGENTS.md`）、跨模块依赖方向（见根 `ARCHITECTURE.md`）

尚未成立（占位）。提供 LLM、PostgreSQL、Redis、供应商 API 等可替换外部实现。把核心端口调用转为 Provider 调用与规范化结果。MVP 不含向量库 / RAG。实现与测试待建，边界以仓库根 `ARCHITECTURE.md` 的 Infrastructure 行为准。
