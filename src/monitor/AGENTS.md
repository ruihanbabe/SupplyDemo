# Monitor 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有指标计算（库存周转率 / 缺货率 / 物流异常等）与 TriggerEvent 生成。由 cron Scheduler 定时驱动；TriggerEvent 进入 Supervisor，走与用户交互相同的路由与审批流程。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Monitor 行与「请求链路」、`DECISIONS.md` D03 / D07 / D11；代码与测试待建。

## 不变量与 contract

- 不含 LLM 调用，纯程序计算。
- 只有越过阈值才生成 TriggerEvent，避免高频空转。
- 阈值只来自 `business_rule` 表（见 `DECISIONS.md` D07），不硬编码。
- 指标计算依赖时间序列数据；已落库的元件/BOM 数据（见 `data/supplychain/normalized/projects.yaml`）只有身份与用量，不含时间序列，需先补合成数据（见 `DECISIONS.md` D11）。

## 修改后验证

`Makefile` 已建（目标见 `make help`），本模块代码与测试待建。届时运行本模块对应的 `make` 目标与单元测试，并按 `DEVELOPMENT.md` 的四层验证（`DECISIONS.md` D14）补充“仅越阈值才产生事件”的确认。
