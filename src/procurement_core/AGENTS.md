# 采购业务核模块指引

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有缺口计算、候选选型、报价计算、方案版本与 content_hash 等确定性业务规则。被 Workflow Controller 的确定性节点调用，不依赖模型、Prompt 或具体 Provider。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的「采购业务核」行、`DECISIONS.md` D04 / D18，以及 `docs/product/requirements.md` 的 FR-02 与 §5 业务规则。

## 不变量与 contract

- 未计算的 required_qty 用 None/NULL 表示；已解决行必须有正数需求量（数据契约见 `docs/spec/data-model.md` §4）。不得把 None 当作零汇总。
- `Offer` 的金额及数量必须是 Decimal；供应商返回结构应由 Tools 校准，不在本模块猜测缺失 MOQ/倍数。
- `generate_plan` 消费显式快照 ID；不得通过省略有缺口的元件形成“完整”方案。`ready_for_review` 不等于批准或可执行。
- 调整哈希内容或序列化时，必须验证存库读回可重建同一哈希，以及采购条件变化会改变哈希。

## 修改后验证

执行 `make compile`、`make lint`、`make test`；持久化行为修改后执行 `make test-integration`。场景覆盖见 `tests/test_demand.py`、`test_candidates.py`、`test_shortage.py`、`test_offers.py`、`test_plans.py`。集成场景只使用隔离 schema 中显式模拟的数据，不得把真实规范化层批量标记为已核验。
