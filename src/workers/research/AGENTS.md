# Research Agent 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有对 Detail 采集结果的语义推理与归纳建议（ResearchFindings），每条发现绑定 evidence_ref。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Research Agent 行、`DECISIONS.md` D01 / D06；代码与测试待建。

## 不变量与 contract

- 数值类对比（缺口、报价、去重抵扣等）由采购业务核计算，Research 只判断“哪些差异重要”，不裁定算术正确性。
- 候选歧义等不确定项转人工审核，不做模型间自然语言博弈。
- 用旗舰模型（见 `DECISIONS.md` D06）。

## 修改后验证

`Makefile` 与测试待建。建立后运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的三层验证补充“发现必须可追溯到 evidence”的确认。
