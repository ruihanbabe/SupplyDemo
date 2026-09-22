# SupplyAgent HTTP 接口契约

> **文档契约** · 类型：HTTP 契约 SSOT · 读取：实现或调用端点时定点读取
> 更新：路径、包络、错误映射或事务边界变化时
> 独占：HTTP 端点与传输语义
> 不收录：工具协议、状态迁移、界面布局和实现进度

## 1. 响应包络

所有 JSON 响应包含 `schema_version`、`status`、`data`、`warnings`、`error` 和 `trace_id`。status 取 `ok | partial | not_found | error`；not_found 与调用失败必须区分。trace_id 同时写入 `x-trace-id`。

数量与金额双向使用十进制字符串。JSON float 输入返回 400 `validation_failed`。时间戳必须含时区。

## 2. 错误与事务

| 场景 | HTTP | error.code |
|---|---:|---|
| DTO/schema 失败 | 400 | `validation_failed` |
| 业务前置条件失败 | 400 | `invalid_request` |
| 对象不存在 | 404 | `not_found` |
| 鉴权/授权失败 | 401/403 | `unauthorized` / `forbidden` |
| 版本或哈希冲突 | 409 | `version_conflict` |
| 能力尚未接入 | 501 | `not_implemented` |
| 未预期异常 | 500 | `internal_error` |

一次请求一个数据库事务。返回 4xx/5xx 的请求不得留下部分写入。长时间 Run 的每个节点使用独立短事务与 checkpoint，不让一个 HTTP 请求持有全程事务。

## 3. 已落地代码、待 Feature 验收的确定性端点

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/health` | 进程存活 |
| GET | `/api/projects` | 项目注册表 |
| GET | `/api/projects/{project_id}/bom` | BOM 与候选 |
| POST | `/api/demands` | 创建需求并展开 BOM |
| GET | `/api/demands/{demand_id}` | 需求、明细与未解决项 |
| POST | `/api/runs` | 为需求创建 Run |
| GET | `/api/runs/{run_id}` | Run 当前状态 |
| POST | `/api/runs/{run_id}/shortages` | 计算并保存缺口快照 |
| POST | `/api/runs/{run_id}/plans` | 生成确定性方案版本 |
| GET | `/api/demands/{demand_id}/plans` | 方案版本列表 |
| GET | `/api/plans/{plan_id}` | 方案规范内容 |

这些端点只写内部业务记录，不产生外部副作用。

代码与测试已存在，但 `docs/features.json` 的 F08 仍为 `planned`：Feature 状态只能由用户按四层验证授权流转（见 `DECISIONS.md` D14）。在 F08 置为 `passing` 前，本节不构成“已验收”的依据。

## 4. Harness 与调查端点（目标契约）

未实现端点返回 501，不返回假数据或空成功。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/investigations` | 以 demand_id 或结构化 shortage 输入启动调查 |
| POST | `/api/runs/{run_id}/resume` | 提交人工回答或选择，恢复同一 Run |
| POST | `/api/runs/{run_id}/cancel` | 取消 Run；不冒充外部回滚 |
| GET | `/api/runs/{run_id}/events` | SSE 事件流 |
| GET | `/api/runs/{run_id}/evidence` | Evidence 列表、来源和新鲜度 |
| GET | `/api/runs/{run_id}/artifacts` | 风险、调查摘要与建议产物 |
| GET | `/api/runs/{run_id}/context/{bundle_id}` | ContextBundle manifest；不返回密钥或隐藏思维 |
| GET | `/api/runs/{run_id}/trace` | 节点、模型与工具调用诊断 |
| POST | `/api/plans/{plan_id}/decisions` | 对指定版本和 content_hash 评审 |
| POST | `/api/plans/{plan_id}/executions` | 经审批创建草稿，要求 logical_action_id |
| GET | `/api/executions/{logical_action_id}` | 核对外部副作用 |

`POST /api/investigations` 不接受自由指令直接指定工具或模型；服务端根据业务意图和工作流版本选择节点。自然语言可作为补充说明，但不能替代结构化需求。

## 5. SSE 事件

事件类型固定为 `run_state`、`node_started`、`node_completed`、`tool_status`、`model_status`、`human_required`、`artifact_ready` 和 `run_finished`。事件只发送可展示摘要、引用和 trace_id；供应商完整 payload、模型原始隐藏内容和凭据不进入事件流。

断线重连使用 event_id 从已持久化事件继续。连接断开不取消 Run。

## 6. 待定项

| 编号 | 待定内容 |
|---|---|
| H-TBD-01 | 首期是否提供自然语言入口 |

本文还依赖以下跨文档未决项，正文见 `docs/OPEN-QUESTIONS.md`：**Q-03**（外部草稿端点启用时间与目标系统）、**Q-08**（身份认证、角色与 Evidence 原文脱敏策略）。

多租户不在未决之列：已决为带 `tenant_id` 字段作架构预留、MVP 不实现隔离，见 `docs/product/requirements.md` §8。
