# SupplyAgent 界面契约

> **文档契约** · 类型：契约层 · 读取：实现某个视图或交互时按视图名定点读，禁止通读
> 更新：视图清单、状态到交互的映射、不确定性标注规则或流式交互模型变更时由 Claude 写入
> 独占：视图清单与各自消费的端点、`unresolved_reason` 与 `warnings` 到交互的映射、不确定性标注规则、流式与中断的交互模型
> 不收录：端点形状与响应包络（见 `docs/spec/http-api.md`）、界面应包含哪六处的产品层要求（见 `productinfo.md` §11）、状态取值与迁移（见 `docs/spec/state-machine.md`）、业务规则条款（见 `docs/product/requirements.md`）、技术选型理由（见 `DECISIONS.md` D19）

本文定义**怎么呈现**；`http-api.md` 定义**传什么**。技术栈为 Vue 3，界面按 `productinfo.md` §11 的六处组织。设计思路参考外部项目的交互模式但不复用其代码，理由与边界见 `DECISIONS.md` D19。

## 1. 首要约束：界面不得制造确定性

这是全部呈现规则的总纲，来自 `productinfo.md` §8：

- **查不到 ≠ 零，也 ≠ 停产。** 后端返回 `not_found` 或 `stock_unknown` 时，界面显示「未查到」，**不得渲染为 0、`—` 或空白**——空白会被读成「没有缺口」。
- **未核验 ≠ 已核验。** `identity_status` 非 `verified` 的元件，界面必须带可见标记，不能只在 tooltip 里。
- **模拟 ≠ 真实。** `is_simulated` 为真的数据必须在视图层标注，且该标注随数据一起进入导出与截图。
- **部分就绪 ≠ 就绪。** 包络 `status` 为 `partial` 时，整单不得出现「就绪」「可执行」一类表述（BR-10）。

违反以上任一条，即便数字算对了也算界面缺陷。

## 2. 视图清单

| 视图 | 消费端点 | 职责 |
|---|---|---|
| 对话主界面 | `POST /api/chat/stream`（预留） | 自然语言入口，流式呈现进展与最终报告 |
| 需求提交 | `GET /api/projects`、`GET /api/projects/{id}/bom`、`POST /api/demands` | 选项目、填数量与需求日期、对多候选行选型 |
| 任务详情 | `GET /api/demands/{id}`、`POST /api/runs/{id}/shortages` | 逐行展开结果、缺口与未解决项 |
| 风险清单 | `GET /api/monitor/alerts`（预留） | 越阈值的风险，按可挽救价值排序 |
| 来源查看 | `GET /api/runs/{id}/trace`（预留）、方案行内的 `evidence_ref` | 每个结论回指其证据与采集时间 |
| 审批对比 | `GET /api/demands/{id}/plans`、`POST /api/plans/{id}/decisions`（预留） | 版本间差异对比、批准/驳回/要求修改 |
| 草稿结果 | `POST /api/plans/{id}/executions`、`GET /api/executions/{id}`（预留） | 草稿标识与读回核对结果 |

标「预留」的端点在业务逻辑落地前返回 501。**界面对 501 的处理是显示「该能力尚未接入」，不是显示空列表**——空列表会被误读为「没有风险」。

## 3. 展开结果的交互映射

`unresolved_reason` 是业务逻辑与界面之间最重要的接缝。它的五个取值由 `procurement_core` 产生，界面据此决定渲染哪种交互，**双方都不得自行发明新取值**：

| `unresolved_reason` | 含义 | 界面该做什么 |
|---|---|---|
| `null` | 该行已解决 | 正常显示元件与需求量 |
| `quantity_basis_unverified` | 单件用量口径未核验，未参与乘算（BR-01） | 标为待核验，**不显示任何推算出的需求量**，提供「确认数量口径」入口 |
| `missing_candidate` | 源 BOM 无 MPN，无候选可选 | 标为未解决，显示原始位号与描述，提供人工补录入口；不得猜测型号 |
| `candidate_selection_required` | 多候选未选定（BR-02） | 展开候选列表供人工选择，**逐条显示原始厂商与完整 MPN，不得裁剪后缀**（EV-04） |
| `candidate_identity_unverified` | 已选候选身份未核验 | 允许继续，但该行与其下游结论全程带未核验标记 |

选型提交后走 `POST /api/runs/{id}/resume`（预留）继续同一个 Run，不新建 Run——新建会丢失已有的证据链与预算账。

## 4. warnings 的呈现

`warnings` 是后端已经判定的保留意见，界面负责让它可见，**不负责解释掉它**。不得折叠进「详情」里默认不展示。

| warning | 呈现要点 |
|---|---|
| `advisory_only_no_reservation` | 明示这是建议，未占用库存（BR-05） |
| `recheck_before_execution` | 明示执行前会重新核对 |
| `resource_competition` | 明示有其他需求在争用同一批库存（EV-02） |
| `inventory_snapshot_missing` | 该元件无库存快照，缺口不可信，不得显示为「缺口 = 需求量」 |
| `allocations_exceed_on_hand` | 占用超过实物，数据本身有问题，提示人工核查 |
| `price_unknown` / `stock_unknown` / `lead_time_unknown` | 对应字段显示「未知」，不填 0，不填「—」 |
| `insufficient_stock` | 供应商库存低于建议采购量 |
| `identity_unverified` | 元件身份未核验 |
| `tax_and_shipping_excluded` | 金额旁常驻说明，**不得出现「总价」「到岸价」字样**（BR-06） |
| `manual_offer_selection` | 明示报价由人工选定，系统未自动择优 |
| `no_purchase_required` | 该元件无缺口，无需采购 |

多币种结果**不得并排显示为可比数字**，也不得自动折算——币种与包装不同即不可直接比较（BR-06）。

## 5. 流式与中断

对话主界面按事件流渲染，不等全部完成再一次性显示：

1. **进展事件**：Supervisor 的路由决策、各 Worker 的开始与结束、工具调用的发起与返回。工具调用以可折叠面板呈现，默认折叠，展开可见请求与返回摘要及 `trace_id`。
2. **中断事件**：Run 进入 `needs_input` 或 `awaiting_approval` 时，流暂停并在对话中就地渲染交互卡片——前者按 §3 的映射渲染补录或选型，后者渲染审批对比。**中断不是错误**，不得渲染成失败态。
3. **恢复**：用户在卡片上提交后调 `resume`，同一条对话继续，不另起会话。
4. **最终报告**：按 `productinfo.md` §7 的七段结构渲染，段序不得重排、不得省略「风险与不确定项」与「限制说明」两段。

连接中断时显示「连接已断开」并提供重连，**不得把已收到的部分结果当作完整结论**。

## 6. 审批对比视图

审批是本项目的核心卖点，该视图的要求高于其他视图：

- 必须显示被审批方案的 `content_hash` 与版本号，且与后端返回值逐字一致。
- 版本间对比必须逐行显示差异项（供应商、商品、数量、金额、关键条件），不得只显示「有变更」。
- 提交审批时回传用户实际看到的 `content_hash`；**服务端比对不一致即拒绝**。界面不得在提交前自行刷新哈希——那会让用户批准了一份没看过的方案（BR-08、EV-19）。
- 驳回与要求修改必须可填理由，理由进审计记录。
- **界面上不得存在任何「跳过审批」「自动批准」入口**，包括调试开关（BR-09）。

## 7. 术语

界面对运营人员只说业务语言。`productinfo.md` §11 已规定内部框架名称不占据主要用户流程——Supervisor、Worker、Run、TriggerEvent 这类词汇不出现在主流程文案里；Trace 视图作为面向排查的次级界面可以使用。业务术语定义见 `docs/product/GLOSSARY.md`。
