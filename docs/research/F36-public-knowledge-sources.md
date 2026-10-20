# F36 公开知识来源目录

> 核验日期：2026-10-20。本文只保存来源、可验证事实摘要和拟用场景，不复制原文。原网页或 PDF 仍是引用权威；导入后的版本必须保存来源 URL、来源修订和内容哈希。

## 产品与供应知识

| 来源 | 可验证事实摘要 | SupplyAgent 用途 |
|---|---|---|
| [Great Scott Gadgets：HackRF 硬件版本](https://github.com/greatscottgadgets/hackrf/blob/main/docs/source/list_of_hardware_revisions.rst) | HackRF One 的多个版本因元件可得性在 SKY13350/SKY13453、MAX2837/MAX2839 之间调整；版本变化仍需满足产品性能规范。 | 演示“缺料影响特定产品版本”，并证明替代料不是只查 SQL 型号即可批准。 |
| [Great Scott Gadgets：关键硬件组件](https://github.com/greatscottgadgets/hackrf/blob/main/docs/source/hardware_components.rst) | 官方文档把 MAX2837、MAX2839、RFFC5072 等列为 HackRF 平台的关键器件，并给出不同硬件版本的使用关系。 | 建立产品、BOM 与官方器件文档之间的引用链。 |
| [Skyworks：SKY13453-385LF Datasheet](https://www.skyworksinc.com/-/media/SkyWorks/Documents/Products/1901-2000/SKY13453-385LF_202830G.pdf) | 官方 Datasheet 定义该料件为 0.01–6.0 GHz 单控制 SPDT 射频开关；具体电气值必须引用页码和文档版本。 | 产品事实 Specialist 的页级 citation；禁止模型自行生成电气数值。 |
| [Analog Devices：MAX2837 Datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX2837.pdf) | 官方 Datasheet 是 MAX2837 技术规格的权威来源。 | 将 BOM 料号与不可变 PDF 版本、页码和片段哈希关联。 |

## 电子元件销售与采购规范来源

| 来源 | 可验证事实摘要 | 可转化的演示规范 |
|---|---|---|
| [DigiKey：Terms and Conditions](https://www.digikey.com/en/terms-and-conditions) | 网页价格和产品信息可能变化；交付日期是估计而非保证；订单仍需验证和接受；工程样品不得当作量产器件。 | 销售不得把网页库存、价格或预计交期表述为确定承诺；工程样品不得进入量产方案。 |
| [DigiKey：Quality Standards](https://www.digikey.com/en/resources/quality) | DigiKey 公布其 ISO 9001:2015 质量体系和授权分销相关资质。 | 报价比较除价格外必须保留渠道类型和质量来源。 |
| [Mouser：Sales Terms and Conditions](https://www.mouser.com/en/saleterms/) | 报价、价格和库存信息可变化；订单接受以确认或实际履行为准；部分器件存在退货和包装限制。 | Proposal 必须记录报价有效期、订单确认状态、退货限制和附加费用未知项。 |
| [Mouser：Quality Policy](https://www.mouser.com/en/quality/) | Mouser 公布其授权渠道、追溯和防伪控制，并说明 AS6496、AS9120B 等质量体系。 | 未授权渠道不能与授权渠道只按价格等价排序；需要追溯或人工风险审批。 |
| [ECIA：Quality](https://www.ecianow.org/quality/) | ECIA 汇总授权分销、防伪、追溯和风险分析相关标准与检查材料。 | 为渠道真实性、追溯要求和可疑器件隔离建立行业依据。 |

## 使用边界

- 上述来源只用于事实摘要、检索测试和 citation 演示，不把第三方全文重新发布到仓库。
- 数据集中的 `official` 表示来源为官方公开材料，不表示 SupplyAgent 对实时库存、价格、交付或替代兼容性作保证。
- 业务政策由项目生成并明确标记 `synthetic`；它可以引用上述行业事实，但不能冒充 DigiKey、Mouser、ECIA 或器件厂商的内部政策。
- 联网资料进入知识库前必须固化来源 URL、抓取时间、来源修订、内容哈希和允许展示的摘要；网页变化后旧 Claim 必须待重验。
