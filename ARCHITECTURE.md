# SupplyAgent 架构

> **文档契约** · 类型：契约层 · 读取：实现某模块时按 `## ` 章节定点读，禁止通读
> 更新：模块划分、契约类型、依赖方向或不变量变更时
> 独占：编排模式、模块职责与落点、契约类型、依赖方向、并行与失败语义、权限分层、存储分工、不变量
> 不收录：需求条款（见 `docs/product/requirements.md`）、决策理由（见 `DECISIONS.md`）、当前状态（见 `PROGRESS.md`）、表结构（见 `docs/spec/data-model.md`）

## 1. 三种编排模式

不是单一架构，而是按子场景选模式。**「一个项目里用了两种通信模式，且能说清为什么这里同步那里异步」是本项目的核心论点**（T02）。

```
入口（Web / CLI）
      │  转成同一结构化请求
      ▼
┌─────────────────┐
│   Supervisor    │  会话状态、意图路由、预算分配、分派留痕
└────────┬────────┘
         │
         ▼
   ┌──────────────────────────────────────────┐
   │  Graph 编排器（确定性 DAG，自研）          │   ← 同步：fan-out / fan-in
   │                                          │
   │  缺口计算                                 │
   │      │ fan-out：元件 × 核验类型            │
   │      ├── 内部库存/在途   (service, skill) │
   │      ├── 外部报价/供货   (service, MCP)   │
   │      └── 技术规格核验    (agent, LLM)     │
   │      │ fan-in：证据完整性校验 (agent)      │
   │      ▼                                   │
   │  方案生成 (agent + 确定性回填)             │
   └────────┬─────────────────────────────────┘
            │                    ╲
            ▼                     ╲ 异常发出 RiskEvent
      Human Gate（审批）            ╲     ← 异步：事件流
            │                       ▼
            ▼                 ┌───────────────┐
        外部草稿创建           │  预警流        │  声明式 matcher → actions
                              │  通知 / 升级级别│
                              └───────────────┘
```

| 子场景 | 模式 | 为什么 |
|---|---|---|
| 对话入口 | Supervisor 意图路由 | 请求形态不定，需要从冻结枚举中选 workflow |
| 采购核验 | 确定性 Graph（同步 fan-out/fan-in） | 步骤已知且有依赖，必须等全部分支汇合才能出建议 |
| 预警 | 事件驱动（异步） | 不阻塞主流程；一个异常可触发多个互不相关的响应 |

## 2. 模块

| 模块 | 落点 | 职责 | 不变量 |
|---|---|---|---|
| 渠道适配 | `src/api/channels/` | Web / CLI 输入转成同一结构化请求 | 核心不感知入口形态；适配层不含业务逻辑 |
| API | `src/api/` | HTTP DTO、鉴权、响应包络、事务边界 | 不含业务算术或模型编排；精确数值不经 float |
| Supervisor | `src/supervisor/` | 会话状态、意图路由、预算分配、分派与留痕 | 不推理业务、不改写 Worker 结果；意图从冻结枚举中选，未命中即 `unknown_intent` 转人工 |
| Graph 编排器 | `src/orchestration/` | DAG 定义、并发调度、fan-in 汇合、失败语义、预算沿边扣除 | 图用 Python 定义（D23）；节点选择由图与状态决定，不由模型决定；不引入工作流框架 |
| Workers | `src/workers/<name>/` | 单个节点的实现，agent 或 service | 包装为可调用工具；Worker 之间零直接调用；只传类型化结果 |
| 预警流 | `src/alerting/` | `RiskEvent` 的接收、去重与声明式响应分派 | 响应规则是配置不是代码；不阻塞主流程；采集失败不产生也不关闭告警 |
| 权限层 | `src/permissions/` | 分层策略链求值与 `explain` | 下层只能收紧；服务端强制，不靠 prompt |
| Model Gateway | `src/harness/models/` | 统一模型端口、能力协商、能力探测、用量上报 | 能力缺失显式拒绝，不删约束降级；Provider 差异不改变业务权限 |
| Tool Registry | `src/tools/` | 工具注册、schema、按场景与权限注入、调用分派 | 模型只能调用被注入的工具；写权限由服务端强制 |
| 采购业务核 | `src/procurement_core/` | BOM 展开、候选规则、缺口、MOQ、金额、版本与哈希 | 算术与硬规则不交给模型；未知不用零代替 |
| Evidence Ledger | `src/persistence/` | 证据、产物、引用与版本关系 | 只插入；取代用 `superseded_by` 回填；实质性结论必须可回指 |
| Human Gate | `src/runtime/human_gate/` | 审批、驳回、要求修改、恢复 | 等待不是错误；审批绑定方案版本与内容哈希 |
| Run Manager | `src/runtime/run/` | Run 生命周期、暂停、恢复、checkpoint | PostgreSQL 保存恢复必需事实；恢复不重放已完成副作用 |
| Trace | `src/observability/` | 树状调用轨迹、三类日志分离 | 并行分支在轨迹中是树；凭据与原文不进轨迹 |
| Eval Harness | `tests/evals/` | 固定数据集、回放、失败用例登记、回归门禁 | 模型不得看到隐藏答案；门禁阻止合并 |
| Infrastructure | `src/infrastructure/` | PostgreSQL、Redis、模型后端、MCP 客户端 | 凭据不进入配置、轨迹或工具结果 |

### Worker 名册

| Worker | 形态 | 工具集特征 | 模型档 |
|---|---|---|---|
| `internal` | service | 进程内 skill，只读内部库存/在途 | — |
| `sourcing` | service | MCP，只读外部报价/供货 | — |
| `spec_check` | **agent** | datasheet 读取与规格比对，**无任何写权限、不可读内部库存** | 旗舰 |
| `evidence_check` | **agent** | 只读证据账本，**不可出网** | 中档 |
| `proposal` | **agent** | 只读计算结果，**不可出网**；数值由确定性代码回填 | 中档 |
| `action` | service | 外部写入，须 PermissionDecision | — |

三个 agent 的工具集两两不同，且拒绝理由来自不同策略层——这是 `explain` 有东西可解释的前提（T04）。

## 3. 契约类型

跨层只传类型化对象，不以自然语言段落协商：

```text
Request            入口归一后的结构化请求
Run                一次执行的生命周期
GraphNode          节点定义：worker、输入引用、依赖边、超时、预算份额
NodeResult         节点产出：status(ok|partial|not_found|error)、结果引用、用量、耗时
RiskEvent          source(rule|model_judgment|collection_failed)、严重级别、
                   触发规则或证据引用、关联 run_id 与元件
Evidence           值、来源、取得时间、定位块、provenance
Proposal           采购建议，版本化，绑定 content_hash
PermissionDecision 审批结果，绑定方案版本、哈希与动作范围
ModelRequest       call_id、model、消息、工具、输出 schema、预算、超时
ModelResult        文本、结构化输出、工具请求、完成原因、用量、后端
ModelCapabilities  探测得出的实测能力，非手写声明
ToolCall/ToolResult
```

## 4. 依赖方向

```text
Web / CLI → 渠道适配 → API → Supervisor
Supervisor → Graph 编排器 → Worker（节点）
Worker ↛ Worker                       零直接依赖，一律经编排器分派
Worker(service) → Tool Registry → 进程内 skill / MCP 客户端
Worker(agent)   → Model Gateway → ModelBackend（云端 / 本地 / 回放）
Worker(agent)   → Tool Registry（按权限注入的子集）
Graph 编排器 → Evidence Ledger、Run Manager、Human Gate
Graph 编排器 ⇢ 预警流                  单向异步投递，不等待、不回读
权限层 ← Tool Registry、Model Gateway   调用前求值
以上模块 → PostgreSQL
Supervisor / Run Manager → Redis（锁与短期协调，丢失可重建）
Eval Harness → Model Gateway、Tool Registry、Evidence Ledger（只读或隔离 fixture）
```

禁止的反向依赖：

- 采购业务核不依赖模型、Prompt、MCP 或具体 Provider；
- ModelBackend 不读业务库、不自行决定工具权限；
- 工具适配器不实现全局重试或业务审批；
- 预警流不回写主流程状态；
- Redis 不保存唯一的恢复事实；
- 外部返回内容不进入编排的控制条件。

## 5. 并行与失败语义

**部分失败是本设计的核心，不是边角。**一个不处理部分失败的 fan-in，技术上等于没做并行。

fan-out 的并发单位是 **元件 × 核验类型** 的二维展开：3 个缺料元件 × 3 类核验 = 9 个并发节点，落在 3 个 Worker 上。

| 情形 | fan-in 的处理 |
|---|---|
| 全部 `ok` | 正常产出建议 |
| 任一 `partial` / `error` / 超时 | 仍产出建议，但标注缺哪一项、为何缺；发 `RiskEvent(source=collection_failed)`；**抬高审批级别** |
| 任一 `not_found` | 与 `error` 分开记录——「查了没有」与「没查成」不是一回事 |
| 预算耗尽 | 停止未开始的分支，已完成的保留，显式说明哪些元件未核验 |

失败分支已消耗的预算不退。节点重试只在编排层发生，适配器内部不自行循环。

## 6. 权限分层

```
全局策略  →  Worker 策略  →  会话策略
```

下层只能收紧，不能放宽。求值发生在 Tool Registry 注入工具时与调用分派时，两处都执行。

`explain --session S --worker W` 输出该 Worker 在该会话中实际可调用的工具，以及每条允许/拒绝来自哪一层。

三档会话模式：`Explore`（只查询出建议）/ `Ask`（写操作需审批，默认）/ `Auto`（低金额且无 RiskEvent 自动放行）。**`RiskEvent` 强制把会话从 `Auto` 降到 `Ask`。**

## 7. 存储分工

| 数据 | 存储 |
|---|---|
| BOM、元件、库存、在途、需求、方案 | PostgreSQL，权威业务事实 |
| Run、节点 checkpoint、审批、外部动作 | PostgreSQL，恢复与审计事实 |
| 工具/模型调用、证据、RiskEvent | PostgreSQL，只插入或版本化 |
| 阈值规则、预警响应规则 | PostgreSQL `business_rule`，版本化并记录修改人 |
| Prompt、Worker 行为配置、Graph 定义 | Git 管理的配置，不与业务阈值混放 |
| 模型回放录音 | `fixtures/model/`，按请求内容哈希寻址 |
| 官方文档原始字节与抽取文本 | 本地文件系统，`sha256` 内容寻址 |
| 锁、短期缓存、限流协调 | Redis，丢失后可从 PostgreSQL 重建 |
| 原始 BOM / 规范化数据 | `data/supplychain/`，只读单向 |

## 8. 不变量

1. 模型不得产生或覆盖权威数量、金额与状态；
2. 每个实质性结论必须包含可解析的证据引用；
3. `ok` / `partial` / `not_found` / `error` 不得合并；
4. 外部写操作必须经人工批准与执行前复核（`Auto` 档仅在无 `RiskEvent` 且低金额时豁免，且由服务端判定）；
5. 同一 `logical_action_id` 最终最多产生一个外部草稿；
6. 恢复后不重复已完成的副作用；
7. 后端切换不能增加工具权限或绕过 schema；
8. 外部内容只作数据，不作系统指令；
9. Worker 之间零直接调用，一律经编排器分派，且只传类型化结果；
10. 任一分支失败不得在 fan-in 处被当作成功；
11. 真实 / 模拟 / 缓存 / 回放数据在全链路显式标注；
12. 后端能力由探测得出，不由手写声明代替；
13. 预算沿依赖边分配，子任务从父预算扣除，超预算显式停止而非截断证据。

## 9. 当前落点

已实现并验证：`src/procurement_core/`、`src/persistence/`、部分 `src/api/`、`src/contracts/llm.py` 与 `src/infrastructure/llm.py`（统一端口 + 云端后端 + 回放后端）、`src/tools/`（注册与派发雏形）。

未建：`src/orchestration/`、`src/workers/*`、`src/supervisor/`、`src/alerting/`、`src/permissions/`、`src/observability/`、`tests/evals/`。

状态以 `PROGRESS.md` 与 `docs/features.json` 的执行证据为准；目录存在不代表能力完成。

## 10. 待定

| ID | 待定内容 | 不允许提前假设 |
|---|---|---|
| A-01 | 预警响应规则存 YAML 还是 `business_rule` 表 | 不与模型行为配置混放 |
| A-02 | 上下文压缩策略 | 不以「删除旧消息数量」冒充语义摘要 |
| A-03 | 完整外部 payload 的保存位置与保留期 | 不把敏感 payload 写入普通轨迹 |
