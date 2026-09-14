# 采购业务核模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有缺口计算、候选选型、报价计算、审批状态机等确定性业务规则。是分析类查询深度路径（Detail→Research→Summary→Action）的执行内核，被 Detail / Research / Action 复用。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的采购业务核行、`DECISIONS.md` D01 / D07、`productinfo.md` §5 与 §8；FR/BR 条款正文当前仍在归档目录 `data/supplychain/MVP-PRD.md`（尚未提升为正式需求章节，见 `PROGRESS.md` T09），引用时须注意其题头标注为初稿。代码与测试待建。

## 不变量与 contract

- 算术与硬规则（缺口计算、去重抵扣等）由程序实现，LLM 不裁定。
- 候选歧义等不确定项转人工审核，不自动批准。
- 方案版本化并绑定内容哈希；审批与特定方案版本绑定（BR）。
- 业务阈值（如库存告警线）只来自 PostgreSQL `business_rule` 表（见 `DECISIONS.md` D07），不硬编码进代码或 prompt。

## 修改后验证

`Makefile` 与测试待建。建立后运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的三层验证补充缺口 / 报价计算的确定性用例。
