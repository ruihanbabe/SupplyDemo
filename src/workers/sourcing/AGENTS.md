# Sourcing · service

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文
> 更新：本模块责任、不变量或验证方式变更时
> 独占：本模块实现级不变量、修改前置阅读清单、修改后验证命令
> 不收录：顶层责任划分（见 `DECISIONS.md` D02）、架构图、决策理由、当前状态

## 职责

分销商侧并发取证：库存、阶梯价、交期、MOQ。字段型证据。

形态：**service**。划分依据见 `DECISIONS.md` D02。

拥有：多源并发查询、失败三态分类、各源结果独立保存

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的「总体架构图」与「确定性快路径与 Agent 兜底」、`DECISIONS.md` D02、`docs/product/requirements.md` 的 FR-03；BR-06、BR-12、BR-16。

## 不变量与 contract

- 某一来源失败不阻断其余来源。**失败、超时、该来源无此料是三个独立结果**，不得合并。
- 多源分歧各自保留，不取平均、不取最新、不任选其一。分歧的**含义**由 Adjudicator 判定，不在此处裁决。
- 缓存命中保留原始 `retrieved_at` 并标 `provenance=cache`，不刷新为当前时间。
- Worker 之间只传类型化结果，不以自然语言段落互相协商（BR-20）。

## 本模块兜的例外

确定性路径抛出以下 typed 例外时唤起本模块：

- 多源对同一事实给出不同答案
- 全部来源失败

兜不住必须升级人工，不得自行放宽（BR-18）。

## 修改后验证

`make compile`、`make lint`、`make test`；涉及持久化再执行 `make test-integration`。
