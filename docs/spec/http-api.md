# SupplyAgent HTTP 接口契约

> **文档契约** · 类型：契约层 · 读取：实现或调用某个端点时按端点路径定点读，禁止通读
> 更新：端点、响应包络、错误映射或事务边界变更时由 Claude 写入
> 独占：HTTP 响应包络、端点清单与其请求/响应形状、异常到状态码的映射、请求级事务边界、留给业务逻辑的接口位
> 不收录：MCP 工具契约（见 `docs/spec/interfaces.md`）、表结构（见 `docs/spec/data-model.md`）、状态取值与迁移（见 `docs/spec/state-machine.md`）、业务规则条款（见 `docs/product/requirements.md`）、界面呈现（见 `docs/spec/ui-contract.md`）、当前实现进度（见 `PROGRESS.md`）

本文是 UI 与后端之间的唯一接口事实来源。**端点分两类**：已定契约的（承载 F01–F07 的确定性能力）与预留位（业务逻辑尚未接入，形状先定下来，见 §6）。预留位的存在是为了让 UI 可以照着写，而不是等业务逻辑落地后再反推接口。

## 1. 响应包络

所有端点返回同一个包络，字段与 `docs/spec/interfaces.md` 的 `ToolResult` 同构——同一套「证据与不确定性」语义贯穿工具层与 HTTP 层，调用方不必学两套：

```json
{
  "schema_version": 1,
  "status": "ok",
  "data": { },
  "warnings": ["resource_competition"],
  "error": null,
  "trace_id": "8f14e45fceea167a5a36dedd4bea2543"
}
```

| 字段 | 约束 |
|---|---|
| `schema_version` | 整数。包络结构变更时递增，`data` 内部形状变更不递增 |
| `status` | `ok` / `partial` / `not_found` / `error` 四选一，语义见 §2 |
| `data` | 业务载荷；`status` 为 `error` 时必须为 `null` |
| `warnings` | 字符串枚举数组，永不为 `null`；空数组表示无保留意见 |
| `error` | `{code, message, retryable}`；当且仅当 `status` 为 `error` 时非空 |
| `trace_id` | 关联该请求的全部 Trace 记录；同时回写到 `x-trace-id` 响应头 |

`status` 与 `error` 必须一致：`error` 状态却没有 error 对象、或非 error 状态却带着 error 对象，都是契约违约，构造包络时即拒绝。

## 2. status 的四个取值

**业务未命中不是传输错误**，这是本项目反复出现的一条原则（FR-03「查询无结果需明确输出未找到，不得输出库存为零或已停产」）在 HTTP 层的落地：

| 取值 | 含义 | HTTP 码 |
|---|---|---|
| `ok` | 请求完成，结果完整无保留 | 200 |
| `partial` | 请求完成，但结果含未解决项或降级来源。**整单不得据此标为就绪**（BR-10） | 200 |
| `not_found` | 查了，没有。不是错误，`error` 仍为 `null` | 200 |
| `error` | 请求未完成 | 4xx / 5xx |

一个空的查询结果返回 `not_found` 而非 `ok` + 空数组：前者说明「系统查过了且确认没有」，后者容易被 UI 渲染成「零」。

## 3. 精确数值

**数量与金额一律以 JSON 字符串传输，两个方向都是。** BR-03 要求精确数值类型，而 JSON number 在所有主流客户端解析器里都是 IEEE-754 双精度——`0.1` 进去，`0.1000000000000000055` 出来。

- 出参：`Decimal` 序列化为不带引号丢失的十进制字符串，如 `"30.000001"`，不做尾零裁剪。
- 入参：收到 JSON float 一律拒绝，返回 `validation_failed` 并在 message 中说明原因。收到字符串按十进制精确解析。
- 这条约束覆盖 `quantity`、`required_qty`、`shortage_qty`、`suggested_qty`、`unit_price`、`moq`、`order_multiple`、`price_break_qty`、`known_goods_amount`、`budget_total`。

## 4. 错误映射

| 触发源 | HTTP 码 | `error.code` | `retryable` |
|---|---|---|---|
| DTO 校验失败（缺字段、类型错、多余字段、float 数量、无时区时间戳） | 400 | `validation_failed` | false |
| `procurement_core` 抛 `ValueError`（业务前置条件未满足） | 400 | `invalid_request` | false |
| 目标行不存在 | 404 | `not_found` | false |
| 鉴权失败 | 401 / 403 | `unauthorized` / `forbidden` | false |
| 未预期异常 | 500 | `internal_error` | true |

`invalid_request` 的 message 直接来自 `procurement_core`，它指名调用方违反了哪条规则，不泄露存储内部细节。`internal_error` 只回 `trace_id`，细节写服务端日志——**兜底处理器必须实际记录日志**，否则 `trace_id` 指向空无一物。

鉴权失败必须拒绝而非降级放行（见 `ARCHITECTURE.md` API 行的不变量）。

## 5. 请求级事务边界

**一次请求一个事务**：端点要么提交它写的全部内容，要么什么都不留下。`procurement_core` 的 `repository.atomic()` 以保存点嵌套在这个事务内部。

这条约束有可验证的后果：任何返回 4xx/5xx 的请求，数据库中不得留下该请求写入的任何行。典型失败模式是「先插入主表、后续校验失败却已提交」。

## 6. 端点

### 6.1 已定契约

承载 F01–F07 的确定性能力。这些端点只写内部建议记录（`demand` / `run` / `shortage_snapshot` / `plan`），**不产生任何外部副作用**，因此适用 BR-05（建议计算不修改库存、不建立真实预留）而非审批门。

| 方法 | 路径 | 作用 | 对应 |
|---|---|---|---|
| GET | `/health` | 存活探针。不触达数据库——数据库抖动时不应把进程报成已死 | — |
| GET | `/api/projects` | 项目注册表 | — |
| GET | `/api/projects/{project_id}/bom` | BOM 行与候选。候选随行返回，BR-02 的人工选型针对这份清单 | FR-02 |
| POST | `/api/demands` | 提交生产需求并展开 BOM。`demand_id` 由服务端分配 | FR-01、BR-01、BR-03 |
| GET | `/api/demands/{demand_id}` | 需求、展开明细与未解决项 | FR-02 |
| GET | `/api/demands/{demand_id}/plans` | 该需求的全部方案版本 | FR-05 |
| POST | `/api/runs` | 建 Run。租户继承自需求，不接受调用方指定 | — |
| POST | `/api/runs/{run_id}/shortages` | 缺口计算，落 `shortage_snapshot` | FR-02、BR-04、BR-05 |
| POST | `/api/runs/{run_id}/plans` | 生成版本化方案 | FR-05、BR-06～08 |
| GET | `/api/plans/{plan_id}` | 方案读回。返回的是被哈希的那份规范形式 | FR-05、BR-08 |

### 6.2 预留给业务逻辑的接口位

形状先定，实现随对应 Feature 落地。**UI 可以照此编码**，未实现期间这些路径返回 501 与 `error.code = "not_implemented"`，不得返回假数据或空成功。

| 方法 | 路径 | 作用 | 待接入 | 对应 |
|---|---|---|---|---|
| POST | `/api/chat/stream` | 对话入口。请求体含自然语言与可选 `run_id`；以 SSE 逐段返回 Supervisor 的路由决策、Worker 进展、工具调用与最终报告 | Supervisor + Workers | FR-01、D02 |
| POST | `/api/runs/{run_id}/resume` | 中断恢复。用户补齐字段或选定候选后继续同一个 Run，不新建 Run | Supervisor | FR-01、BR-02 |
| GET | `/api/runs/{run_id}` | Run 当前状态与未解决项。状态取值见 `state-machine.md` | Runtime 契约层 | FR-09 |
| GET | `/api/runs/{run_id}/trace` | Agent Trace：本 Run 的全部 LLM 调用与工具调用，含耗时、token、模型版本、升级路由触发规则 | Runtime 契约层 | D11 |
| POST | `/api/plans/{plan_id}/decisions` | 审批门。请求体含 `decision`（approved / rejected / changes_requested）与调用方看到的 `content_hash`；**哈希不匹配一律拒绝**，不得以服务端当前值覆盖 | PermissionDecision | FR-06、BR-08、BR-09 |
| POST | `/api/plans/{plan_id}/executions` | 执行创建采购草稿。必须携带 `logical_action_id`；服务端自行读取批准内容，**不信任请求体里的任何 `approved` 标志** | Action Agent + Tools | FR-07、BR-11 |
| GET | `/api/executions/{logical_action_id}` | 执行结果核对：`found` / `not_found` / `unknown` | Tools | FR-07、BR-11、EV-21 |
| GET | `/api/monitor/alerts` | 当前越阈值的风险清单 | Monitor | FR-08 |
| GET | `/api/components/{component_id}/supply` | 供应查询结果（身份、包装、阶梯、币种、采集时间、缺失字段） | Detail Agent + Tools | FR-03、BR-06、D13 |

预留位上的三条硬约束，实现时不得绕过：

1. **审批不可伪造**：`/decisions` 与 `/executions` 的服务端必须从持久存储读取批准记录并比对 `content_hash`，绝不接受请求体自称已批准（EV-18）。
2. **写操作必经审批门**：`/executions` 之外不得存在任何产生外部副作用的路径；自动触发的 Run 同样走这条链，不因来自 Monitor 而豁免（D03）。
3. **外部返回内容仅作数据**：`/components/{id}/supply` 回传的供应商描述字段可能夹带指令式文本，一律不得据此提权或触发调用（EV-17、D12 约束段）。

## 7. 与 UI 的分工

本文定义**传什么**，`docs/spec/ui-contract.md` 定义**怎么呈现**。两者的接缝是 `warnings` 枚举与 `unresolved_reason` 取值——UI 依据它们决定渲染哪种交互，后端依据业务规则决定何时产生它们，双方都不得自行发明新取值。
