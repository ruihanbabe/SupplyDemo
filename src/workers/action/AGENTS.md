# Action · service

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文
> 更新：本模块责任、不变量或验证方式变更时
> 独占：本模块实现级不变量、修改前置阅读清单、修改后验证命令
> 不收录：顶层责任划分（见 `DECISIONS.md` D02）、架构图、决策理由、当前状态

## 职责

落地写入：告警开关、人工待办、元件主数据核验写回。幂等 + 审批，无判断空间。

形态：**service**。划分依据见 `DECISIONS.md` D02。

拥有：外部与主数据写入的幂等执行与审批门

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的「总体架构图」与「确定性快路径与 Agent 兜底」、`DECISIONS.md` D02、`docs/product/requirements.md` 的 FR-06、FR-07、FR-11；BR-08、BR-11、BR-15。

## 不变量与 contract

- **本目录不得出现模型调用。**写什么由上游判定，此处只负责写得对。
- 写操作必须持有绑定版本与 `content_hash` 的 PermissionDecision；内容变化则重新审批。
- 同一 `logical_action_id` 最终最多产生一个外部对象；结果未知时核对而非重发。
- 告警唯一性由数据库部分唯一索引 `uq_alert_active` 强制，不靠先查后插。
- Worker 之间只传类型化结果，不以自然语言段落互相协商（BR-20）。

## 本模块兜的例外

确定性路径抛出以下 typed 例外时唤起本模块：

- 写入结果未知（超时后响应丢失）

兜不住必须升级人工，不得自行放宽（BR-18）。

## 修改后验证

`make compile`、`make lint`、`make test`；涉及持久化再执行 `make test-integration`。
