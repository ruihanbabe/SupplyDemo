# Query Agent 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有 NL2SQL 生成与只读执行、MCP 外部查询调用。简单路径产出直接喂给 Summary，分析路径产出作为 Detail 的输入。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Query Agent 行、`DECISIONS.md` D02 / D08 / D09；代码与测试待建。

## 不变量与 contract

- 仅生成只读 SELECT；schema 白名单 + AST 校验；不执行写操作。
- schema 暴露安全准则与业务术语语义映射当前暂缓（见 `DECISIONS.md` D09），实现时以届时定稿为准，不自行发明。
- 外部查询走 Tools（MCP），不绕过 Tools 直连外部系统。

## 修改后验证

`Makefile` 已建（目标见 `make help`），本模块代码与测试待建。届时运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的四层验证（`DECISIONS.md` D14）补充只读约束与白名单的对抗性用例。
