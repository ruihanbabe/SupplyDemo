# Manufacturer · agent

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文
> 更新：本模块责任、不变量或验证方式变更时
> 独占：本模块实现级不变量、修改前置阅读清单、修改后验证命令
> 不收录：顶层责任划分（见 `DECISIONS.md` D02）、架构图、决策理由、当前状态

## 职责

厂商官方资料取证：型号寻址、文档取回、参数式解码、span 抽取。文档型证据。

形态：**agent**。划分依据见 `DECISIONS.md` D02。

拥有：DocumentResolver 策略链的 agent 兜底部分、编码器阅读、原文定位

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的「总体架构图」与「确定性快路径与 Agent 兜底」、`DECISIONS.md` D02、`docs/product/requirements.md` 的 FR-04、FR-12；BR-12、BR-17。

## 不变量与 contract

- **寻址而非相似度检索。**不得以向量召回代替型号定位。
- 枚举命中与解码命中的证据强度不同。**仅解码命中时不得断言该型号生命周期、不得发停产告警**，只能 `cannot_confirm`。
- `unresolvable` / `CANNOT_EXTRACT` / `not_found` 三态互不合并——合并任意两个都会把「查不到」变成「已停产」。
- 站内检索 skill 绑定厂商域名白名单，不跟随跨域重定向。
- 原文片段须在记录的定位处逐字命中；`extractor_version` 必填。
- Worker 之间只传类型化结果，不以自然语言段落互相协商（BR-20）。

## 本模块兜的例外

确定性路径抛出以下 typed 例外时唤起本模块：

- URL 模板推导失败
- 站点拒绝自动抓取
- 链接失效（301→404）
- 参数式文档需读编码器表
- 多条命中需消歧

兜不住必须升级人工，不得自行放宽（BR-18）。

## 修改后验证

`make compile`、`make lint`、`make test`；涉及持久化再执行 `make test-integration`。
