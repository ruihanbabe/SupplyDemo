# Tools 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有 Worker 与外部世界之间的统一调用封装：内部进程内确定性操作用轻量 skill，跨系统 / 可复用集成走 MCP。对上层输出标准化 ToolResult。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Tools 行、`DECISIONS.md` D08；代码与测试待建。

## 不变量与 contract

- 内部数据库查询等确定性操作用 skill，不套 MCP 协议开销；跨边界集成走 MCP。
- 模型不得绕过 Tools 层直接调用外部系统。
- 供应商字段映射等实现细节归本层，不下放给 Query / Detail / Research 等 Worker 的推理逻辑。
- 凭据由 Infrastructure 层注入，不出现在本层代码或返回值中。

## 修改后验证

`Makefile` 与测试待建。建立后运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的三层验证补充 skill / MCP 边界与失败降级的确认。
