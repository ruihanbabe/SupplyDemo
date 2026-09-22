# Report · agent

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文
> 更新：本模块责任、不变量或验证方式变更时
> 独占：本模块实现级不变量、修改前置阅读清单、修改后验证命令
> 不收录：顶层责任划分（见 `DECISIONS.md` D02）、架构图、决策理由、当前状态

## 职责

产出组织：比价报告、候选对比、替代建议、告警渲染。只组织文字，事实值由代码回填。

形态：**agent**。划分依据见 `DECISIONS.md` D02。

拥有：报告版本化、面向人的说明文字

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的「总体架构图」与「确定性快路径与 Agent 兜底」、`DECISIONS.md` D02、`docs/product/requirements.md` 的 FR-05、FR-12；BR-17、BR-20。

## 不变量与 contract

- **模型不得撰写事实值。**数量、金额、库存、交期、型号、状态枚举由确定性代码从 Evidence 回填。
- 说明文字中的每个事实必须对应 Evidence；自由文本中的数字须在本次 ContextBundle 实际纳入的证据中逐字命中。
- 多源分歧并排呈现，不合并为单值；未能确认项分类列出，不合并。
- 未知不渲染为零或空白。
- Worker 之间只传类型化结果，不以自然语言段落互相协商（BR-20）。

## 本模块兜的例外

确定性路径抛出以下 typed 例外时唤起本模块：

- 需要陈述的事实缺少 Evidence 支撑

兜不住必须升级人工，不得自行放宽（BR-18）。

## 修改后验证

`make compile`、`make lint`、`make test`；涉及持久化再执行 `make test-integration`。
