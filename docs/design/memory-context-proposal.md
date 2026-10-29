# SupplyAgent Memory / Context 方案入口

2026-10-27：用户已确认方案，并要求整理成开发 Contract。原研究建议的稳定内容已下沉到下列权威位置；本文只作导航，不再维护第二套策略/字段正文。尚未实施，当前状态以 features.json 为准。

| 阅读目的 | 唯一入口 |
|---|---|
| 架构、职责与数据权威 | [spec §2.6](../../spec.md#26-知识记忆与证据) |
| 长期记忆范围、M1–M3 验收与回退 | [F48](../../contracts/F48.md) |
| 会话/Working State/装配/压缩、C1–C3 验收 | [F52](../../contracts/F52.md) |
| 纳入什么、确认/合并/冲突、污染/PII、检索/巩固/遗忘 | [BL09](../../business-logic.md#bl09-memory-选型与治理边界) |
| 短期消息、优先级/预算/渐进读与压缩策略 | [BL13](../../business-logic.md#bl13-短期状态context-装配与压缩f52-目标) |
| 来源/身份/版本/进度/接受时点/删除与物理责任 | [DC18–DC21](../../data-contract.md#dc18-会话消息工作状态与提交水位f52-目标) |
| Mem0 Adapter/捕获发布/对账/清理、代码落点与目标配置 | [Memory 模块](../../src/product_api/MEMORY_RUNTIME.md) |
| Compiler/Store Interface、缓冲/预算/压缩与目标配置 | [Context 模块](../../src/product_api/CONTEXT_RUNTIME.md) |
| 每请求暂停、v3 分块完整性及 Gateway 绑定 | [Pi Context 协议](../../pi_sidecar/CONTEXT_PROTOCOL.md) |
| 框架固定版本、能力/局限、采用成本与翻转条件 | [框架研究](../research/2026-10-27-memory-framework-selection.md) |
| 四类业务的具体合成故事与验收素材 | [业务故事](../scenarios/2026-10-23-electronics-business-stories.md) |

方案采用 Mem0 的提取/检索，结合本项目受控 Python/SQL/Pi 路径；不能用当前薄表缩减完整业务目标。选型依据、上游限制只在研究文件维护，业务规则与数据契约不从第三方 README 推导授权。

本轮仅文档落地，没有安装/部署/模型调用/迁移；验证统一见 F52。后续开发按 F51→F52→F48，一次一个单元；阶段小不表示删除后续目标。
