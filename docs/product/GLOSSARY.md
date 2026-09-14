# SupplyAgent 领域术语表

> **文档契约** · 类型：契约层 · 读取：首次进入项目时通读一次；之后按术语定点查
> 更新：新增领域术语、或既有术语的定义/字段名变更时由 Claude 写入
> 独占：领域术语的定义与中英文对应、数据层字段名与业务含义的绑定
> 不收录：字段的物理类型与约束（见 `docs/spec/data-model.md`）、业务规则正文（见 `docs/product/requirements.md`）、数据现状与缺口（见 `data/supplychain/normalized/projects.yaml`）

统一词汇，避免多 Agent 各自发明字段名。**数据层术语的字段名以 `data/supplychain/normalized/` 的实际列名为准**，本表只做含义绑定，不复制数据现状。

## 1. 数据层（已落库）

| 术语 | 字段名 | 含义 |
|---|---|---|
| 项目 | `project_id` | 一块开源硬件板卡，是 BOM 的归属单位。当前 5 个 |
| BOM | — | Bill of Materials，物料清单。一块板卡生产所需的全部元件与用量 |
| 用料行 | `bom_line` / `line_id` | BOM 中的一行，代表「这块板上这些位置要用这种元件，共几个」。是缺料计算的最小单位 |
| 位号 | `reference` | 元件在 PCB 上的标识（如 `R15`、`IC1`）。一行可含多个，空格分隔 |
| 用量 | `quantity` | 该用料行在**单块板**上的数量。乘生产数量前必须先核验口径（见 BR-01） |
| 源行号 | `source_row_index` | 该行在原始 CSV 中的行号，用于回溯到 `domdata/` 原始层 |
| MPN | `mpn` | Manufacturer Part Number，厂商型号。元件身份的主键要素 |
| 厂商 | `manufacturer` / `manufacturer_raw` | 归一后的厂商名 / 原始层的原始写法。两者都保留，归一不覆盖原值 |
| 候选 | `bom_line_candidate` / `candidate_seq` | 一条用料行可接受的某个具体型号。**一行可有多个候选，跨多家厂商**；候选间无技术等价结论，选型歧义转人工（见 BR-02） |
| 元件 | `component` / `component_id` | 去重后的元件主体，由「厂商 + MPN」确定身份。多条用料行可指向同一元件 |
| 身份状态 | `identity_status` | 元件身份的核验程度。当前全部为 `source_asserted`（仅由源 BOM 断言，未经独立核验）；`review_required`（需人工核验）是 EV-04 预期会出现的取值，核验流程建立后才会产生 |
| 分销商料号 | `bom_line_distributor_sku` / `distributor_sku` | 某分销商（`digikey` / `mouser` / `farnell` / `newark` / `rs`）对该用料行给出的商品编号。**挂在行级不挂在候选级**，多候选行无法判定 SKU 归属哪个候选 |

原始层 `domdata/` 只读不可变，规范化层 `normalized/` 由 `normalize_domdata.py` 单向产出，是下游唯一消费入口（见 `ARCHITECTURE.md`）。

## 2. 业务层（待建表）

| 术语 | 含义 |
|---|---|
| 生产需求 | 用户提交的「产品版本 + 数量 + 需求日期」。不含设备、工序或人员安排 |
| 总需求 | 单件用量 × 生产数量后按物料汇总（见 BR-03） |
| 实物可用库存 | 仓库中可支配的实物数量。**隔离／待检库存不计入** |
| 占用 | 已被其他需求分配掉的库存量。同一批库存不得重复抵扣 |
| 在途 | 已下单未到货的数量。只有「已确认且未分配、到期前能到」的部分计入可分配量；无到货日期的列作不确定项 |
| 可分配量 | 实物可用扣除占用 + 合格在途（见 BR-04） |
| 缺口 | `max(0, 本需求未满足量 − 可分配量)`。由采购业务核以确定性代码计算，LLM 不裁定 |
| MOQ | Minimum Order Quantity，最小起订量 |
| 倍数 | 订购数量必须是该值的整数倍。与 MOQ 共同决定实际建议采购量 |
| 阶梯价 | 按采购数量分档的单价。金额须按「建议数量所在的那一档」计算，不按缺口档 |
| 单源方案 | 只有一个供应来源的采购方案，必须显式标注（见 BR-07） |
| 到岸总价 | 含税运的总成本。**MVP 不承诺**——税费运费未知时明确排除，不伪装完整报价 |
| 采购草稿 | 目标业务系统（ERP）中创建的未提交单据。动作终点，不提交不付款 |

## 3. Runtime 与审计层

| 术语 | 含义 |
|---|---|
| Session | 一次用户会话。持有结构化对话 state，不常驻原始消息历史（见 D05） |
| Run | 一次可恢复的任务执行。用户交互与 Monitor 自动触发产生的 Run 走同一条权限链 |
| ToolCall / ToolResult | 一次工具调用及其标准化返回。是 Agent Trace 的记录单位 |
| PermissionDecision | 写操作前的人工审批门。批准绑定方案内容哈希与版本（见 BR-08） |
| TriggerEvent | Monitor 层指标越阈值时生成的事件，喂给 Supervisor 进入分析路径（见 D03） |
| evidence_ref | 证据引用。每个实质性判断都必须绑定证据来源与数据快照时间 |
| logical_action_id | 逻辑动作键。跨系统写操作按此去重，保证同一动作最终只产生一张草稿（见 BR-11） |
| plan_id | 采购方案标识。写工具只接受受控的 `plan_id`，服务端据此读取批准内容——**不相信模型传来的 `approved=true`** |
| 方案内容哈希 | 方案实质内容的哈希值。审批绑定它；内容变了哈希就变，旧批准失效 |
| trace_id | 贯穿一次请求的关联 ID，用于串起跨层日志 |
| 三层可观测性 | Agent Trace（LLM/工具调用全记录）+ 操作人员日志（审批/驳回/改阈值留痕）+ 开发日志（ADR 与配置变更审计） |
| business_rule | 存 PostgreSQL 的可变业务参数表（如库存告警线），版本化并审计，与 YAML 行为配置分开维护（见 D07） |

## 4. 工程治理

| 术语 | 含义 |
|---|---|
| SSOT | Single Source of Truth。一个信息要素只有一处正文，别处只留编号引用（矩阵见 `CODING_RULES.md`） |
| 文档契约块 | 每份 Markdown 开头的四行 blockquote，规定该文档的读取时机、更新时机、独占信息与不收录信息 |
| Feature | Harness 层的最小可独立验收单元，不是 Agent 内部的任务拆解步骤（见 `DEVELOPMENT.md`） |
| 验证四层 | ① 静态契约 ② 离线测试 ③ 集成故障注入 ④ 授权端到端（见 D14） |
| skill / MCP | 工具封装的两种形态：进程内确定性操作用 skill，跨边界集成走 MCP（见 D08） |
| 归档区 | `data/supplychain/` 下的设计初稿，**不得作为实现依据**；正文已按 D15 迁出到 `docs/` |
