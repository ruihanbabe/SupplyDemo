# SupplyAgent 领域术语表

> **文档契约** · 类型：共享词汇契约 · 读取：首次进入项目时通读，之后定点查询
> 更新：领域对象或统一命名变化时
> 独占：中英文术语及业务含义
> 不收录：物理字段类型、业务规则和当前数据统计

## 业务对象

| 术语 | 含义 |
|---|---|
| Production Demand / 生产需求 | 产品或物料、数量、需求日期和业务范围的结构化需求 |
| BOM | 某产品版本的物料清单 |
| BOM Line / 用料行 | BOM 中一种用料及其单件数量、位号和候选关系 |
| MPN | Manufacturer Part Number，器件身份的主要要素 |
| Candidate / 候选 | 用料行可接受的具体元件；多个候选不表示已证实技术等价 |
| Component / 元件 | 规范化的厂商与 MPN 身份 |
| Availability / 可分配量 | 按版本化规则计算的可用库存与合格在途 |
| Shortage Snapshot / 缺口快照 | 某一时点需求、可分配量、规则版本和缺口的不可变计算结果 |
| Supplier Offer / 供应报价 | 带 SKU、数量、MOQ、包装、价格、币种、交期、时间和来源的观测 |
| Replenishment Proposal / 补充建议 | 针对一个缺口快照生成的版本化采购建议；不等于库存占用或采购单 |
| Monitor Target / 监测对象 | 纳入持续监测的元件或 BOM 行，带阈值与复核策略 |
| Observation / 观察 | 一次采集到的供应事实。数值型来自分销商响应，文档型来自制造商资料并带原文定位 |
| Alert / 告警 | 观察相对阈值或上一状态发生了需要人关注的变化；同一对象同一类型同时最多一条活动告警 |
| Document Resolution / 型号寻址 | 由型号定位到制造商官方文档的过程，输出 resolved / ambiguous / unresolvable 三态 |
| Procurement Draft / 采购草稿 | 经审批后在目标系统创建的未提交单据；不提交、不付款 |

## 调查、证据与风险

| 术语 | 含义 |
|---|---|
| Evidence / 证据 | 带来源、取得时间、查询参数摘要、内容摘要、新鲜度和完整性状态的事实记录 |
| Evidence Set | 当前 Run 中可用于某个判断的一组有效 Evidence |
| evidence_ref | 从 RiskFinding、Artifact 或 Proposal 回指 Evidence 的稳定引用 |
| RiskFinding / 风险发现 | 对当前采购或选型决策有影响的证据化风险；包含类型、严重度、置信度、适用对象和建议动作 |
| Unknown / 无法判断 | 证据不足时的有效结论，必须附缺失信息，不能用臆测填补 |
| Provenance / 来源性质 | real、cache、sample 或 replay，用来防止把模拟数据冒充真实结果 |

## Harness 与 Runtime

| 术语 | 含义 |
|---|---|
| Run | 一次有持久状态的执行：告警复核、库存调研、比价、替代调研或候选对比；可等待、恢复、取消和重放 |
| Workflow Node | Run 的一个步骤，分为 deterministic、tool、model 和 human 四类 |
| ModelBackend | 对具体模型服务的统一能力接口 |
| Context Compiler | 从权威状态、有效证据、产物和人工输入编译模型上下文的组件 |
| ContextBundle | 一次模型调用的结构化上下文及其 manifest；不是无限聊天记录 |
| Artifact / 产物 | 节点产生的版本化结构化结果，如调查摘要、风险集合或建议解释 |
| Tool Registry | 注册工具 schema、权限、副作用、预算与 Adapter 的目录 |
| ToolCall / ToolResult | 一次工具调用和标准化结果 |
| Evidence & Artifact Ledger | 保存证据、产物、引用与版本关系的账本 |
| Human Gate | 等待人工补充、选择、审批或取消的工作流节点 |
| PermissionDecision | 对指定方案版本、内容哈希和动作范围的人工决定 |
| logical_action_id | 外部写操作的业务幂等键 |
| ReplayBackend | 从已记录事件返回结果的模型后端，用于离线回归和诊断 |
| trace_id | 贯穿入口、节点、模型、工具和持久化记录的关联标识 |

## 数据治理

| 术语 | 含义 |
|---|---|
| domdata | 原始只读数据层，不允许修订或补字段 |
| normalized | 从原始层确定性生成、供下游使用的规范化数据 |
| business_rule | PostgreSQL 中版本化的可变业务规则 |
| SSOT | 一个信息要素只在一个权威文档保存正文 |
| Feature | 可独立实现、验证和报告状态的交付单元 |
