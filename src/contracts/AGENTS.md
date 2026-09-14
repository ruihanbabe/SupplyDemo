# Runtime 契约层模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有各层共享的类型化状态：Session / Run / ToolCall / ToolResult / PermissionDecision / RuntimeEvent，以及结构化升级报告类型。这些类型是各 Agent / 模块之间唯一的通信媒介。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Runtime 契约层行与「依赖方向」、`DECISIONS.md` D02 / D03、`DEVELOPMENT.md`「自动修复与升级上限」；代码与测试待建。

## 不变量与 contract

- 各 Agent 间不做自然语言对话，只通过本层类型化状态传递。
- 审计相关记录（Run / ToolCall / PermissionDecision）语义为“只插入不更新”。
- 结构化升级报告统一一处定义、跨模块 import 复用，不得各模块另发明格式；具体字段待设计（见 `AGENTS.md`「全局硬约束」）。
- 本层不依赖任何具体 Provider 或业务规则。

## 修改后验证

`Makefile` 已建（目标见 `make help`），本模块代码与测试待建。届时运行本模块对应的 `make` 目标与类型 / 序列化单元测试，并按 `DEVELOPMENT.md` 的四层验证（`DECISIONS.md` D14）补充跨层契约一致性确认。
