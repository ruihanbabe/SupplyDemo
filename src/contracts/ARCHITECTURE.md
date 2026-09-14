# Runtime 契约层模块

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块职责或代码落点变更时由 Codex 写入
> 独占：本模块在根 `ARCHITECTURE.md` 模块表中那一行的展开说明
> 不收录：不变量正文（见同目录 `AGENTS.md`）、跨模块依赖方向（见根 `ARCHITECTURE.md`）

尚未成立（占位）。拥有 Session / Run / ToolCall / ToolResult / PermissionDecision / RuntimeEvent 等跨层共享类型，以及结构化升级报告类型（具体字段格式待设计）。被所有层依赖，自身不反向依赖业务逻辑。实现与测试待建，边界以仓库根 `ARCHITECTURE.md` 的 Runtime 契约层行为准。
