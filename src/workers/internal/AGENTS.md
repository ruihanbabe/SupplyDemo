# Internal · service

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文
> 更新：本模块责任、不变量或验证方式变更时
> 独占：本模块实现级不变量、修改前置阅读清单、修改后验证命令
> 不收录：顶层责任划分（见 `DECISIONS.md` D02）、架构图、决策理由、当前状态

## 职责

纯确定性内部核算：可分配量、缺口、齐套率、单源判定。不调用模型。

形态：**service**。划分依据见 `DECISIONS.md` D02。

拥有：基于 PostgreSQL 内数据的全部确定性计算

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的「总体架构图」与「确定性快路径与 Agent 兜底」、`DECISIONS.md` D02、`docs/product/requirements.md` 的 FR-02；BR-01～05、BR-10。

## 不变量与 contract

- **本目录不得出现模型调用。**任何需要判断的内容抛 typed 例外交给 agent，不在此处近似。
- 未计算与零是两回事；未知用 NULL，不用 0 汇总。
- 可分配量必须扣除其他需求的占用，不等于账面库存。
- Worker 之间只传类型化结果，不以自然语言段落互相协商（BR-20）。

## 本模块兜的例外

确定性路径抛出以下 typed 例外时唤起本模块：

- 库存快照过期到无法支撑判定
- 同一元件在多个需求间的占用冲突

兜不住必须升级人工，不得自行放宽（BR-18）。

## 修改后验证

`make compile`、`make lint`、`make test`；涉及持久化再执行 `make test-integration`。
