# Action Agent 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有执行指导生成，以及工单创建 / 通知发送 / 采购草稿等写操作的发起。产出是“待审批的写操作请求”，不是已执行的副作用。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Action Agent 行与「依赖方向」、`DECISIONS.md` D02 / D03；`src/contracts/AGENTS.md`；代码与测试待建。

## 不变量与 contract

- 所有写操作必须先过 PermissionDecision 审批门；不得因 Monitor 自动触发就绕过审批。
- 自动生成的方案必须挂起等待人工审批，不得绕过业务校验直接交付。
- 写操作经 Tools 层发起，不绕过 Tools 直连外部系统。

## 修改后验证

`Makefile` 已建（目标见 `make help`），本模块代码与测试待建。届时运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的四层验证（`DECISIONS.md` D14）补充“未审批不得产生外部副作用”的端到端确认。
