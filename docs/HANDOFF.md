# SupplyAgent 交接文档

> **类型**：交接层 · **读取**：新会话第一条，全文读
> **独占**：求职技术点覆盖矩阵、三个参考来源的借用边界、被否决方案及原因、开工前阻塞项
> **不收录**：任何已有 SSOT 的正文。决策见 `DECISIONS.md`，架构见 `ARCHITECTURE.md`，需求见 `docs/product/requirements.md`，队列见 `docs/features.json`，未决项见 `docs/OPEN-QUESTIONS.md`，状态见 `PROGRESS.md`

本文只解决一件事：**让一个零上下文的新会话，不重走已经走过的弯路。**

---

## 1. 项目与现状

SupplyAgent 是**元件供应风险主动预警系统**。事件驱动（提交 BOM/需求即主动暴露问题），围绕告警提供决策支持：多源库存调研、比价建议、官方推荐替代、多候选对比。

项目的第二重目的是**求职作品**：覆盖多 Agent 开发岗位要求的完整技术栈。当业务价值与技术覆盖冲突时，以技术覆盖优先——这是刻意的取舍，不是设计失误。

```
真有代码：api  contracts  infrastructure  persistence  procurement_core
纯文档占位：7 个 worker 目录、supervisor、monitor、tools
未建目录：src/harness/  src/runtime/  src/semantic/  src/alerting/
队列：24 个 Feature，F01–F07 passing，其余 planned
验证：①编译 ✓  ②离线 108 ✓  ③集成 22 ✓  ④未执行
```

**Harness 一行代码都还没有。**曾实现过 F10–F16 又按用户要求整体回退，回退是干净的。

---

## 2. 求职技术点覆盖矩阵

这是本项目存在的理由。**任何改动都不得让某一行退化为"无落点"。**

| # | 技术点 | 项目落点 | Feature | 状态 |
|---|---|---|---|---|
| 1 | 多 LLM 后端接入、能力协商、显式降级 | Model Gateway | F10 | 有落点 |
| 2 | 统一工具执行链、错误分类、重试收口 | Tool Registry | F13 | 有落点 |
| 3 | 工具并发的乱序 / 重复 / 冲突 | Sourcing 多源并发 + Run 幂等 | F19、F15 | 有落点 |
| 4 | 任务暂停 / 恢复 / 结果回传 | Run Manager + node checkpoint | F15、F16 | 有落点 |
| 5 | **JSONL 会话恢复** | — | — | **缺口，见 §5** |
| 6 | 证据时间戳、来源绑定、取代链 | Evidence Ledger | F12 | 有落点 |
| 7 | Eval：固定数据集 / 失败用例登记 / 回归门禁 | Replay/Eval Harness | F20 | 有落点 |
| 8 | Trace 与可观测 | trace_id 贯穿；字段集未冻结 | F20 | 部分 |
| 9 | 权限与审批（服务端强制） | ToolRegistry + PermissionDecision | F13、F16 | 有落点 |
| 10 | 四层验证（契约/离线/故障注入/授权端到端） | `DECISIONS.md` D14 | 全部 | 有落点 |
| 11 | 工程约束与文档纪律 | `CODING_RULES.md` + 全局不变量 | — | 有落点 |
| 12 | 多 Agent 模式（Supervisor-Workers） | Supervisor 条件路由 | F22 | 有落点 |
| 13 | 业务知识接入（供应链术语 LLM 不理解） | Semantic Layer | F21 | 有落点 |
| 14 | 确定性快路径 + Agent 兜底 | Exception Router | F23 | 有落点 |
| 15 | Skill / MCP 工具接入 | ToolRegistry 已留位 | F13 | **弱，需加强** |
| 16 | 多 provider 封装（库存/价格） | Sourcing Worker | F19 | 有落点 |
| 17 | 多源数据入库（BI/ERP 方向） | normalized 导入 | F02 | 有落点（只读，单向） |

---

## 3. 三个参考来源的借用边界

### 3.1 Craft Agents —— 借骨架，不借执行引擎

**借**：`AgentBackend` provider-agnostic 事件抽象、工具工厂、`Permission / PreToolUse` 权限层、`Usage / Diagnostics`、统一协议下的多端复用。这几块与本项目的 Model Gateway、Tool Registry、审批层、成本追踪一一对应。

**不借**：`BaseAgent` 的自由运行循环。它的生命周期是「模型决定下一步 → tool → 回灌 → 循环」，而本项目要求节点选择由状态与显式规则决定（`DECISIONS.md` D02）。照抄会让模型拿到控制权，顺手改写权威数值。

### 3.2 MiniClaw —— 只借两块侧翼

**借**：可靠性侧翼（`Inbox → Turn → Outbox`、暂停/恢复、幂等投递、超时后先核验副作用再重试）；安全边界（权限层与执行隔离互不替代）。

**不借**：多渠道 IM 入口、Electron 桌面端、Workspace 文件执行、Memory v2、长期在线个人助手。这些全部落在 `productinfo.md §6 非范围` 内——MiniClaw 解决的是「一个 agent 活很久、到处能被找到」，本项目是「一个任务活很久、在一处被审批」。

### 3.3 Amazon 零售多 Agent 实践 —— 借实现路径，不借责任划分

**借**：Supervisor 作为唯一有状态的上下文枢纽；**按复杂度条件路由**而非每次跑完整链路；**子 Agent 包装成可调用工具**，把「多智能体协作」降维成「工具调用」，使编排与实现解耦。

**不借**：它的 `Query / Detail / Research / Summary / Action` 五角色。那是为零售销售分析设计的（滞销品、周转、渠道品类下钻）。本项目的责任由自己的六项业务能力反推得出，见 `DECISIONS.md` D02。

**关键差异**：本项目多出一个 **Adjudicator（判定）** 责任，被 4 项业务能力复用，是系统内核；Amazon 那套没有它，因为零售场景的判定就是阈值比较，而此处判错停产会让人用错料。反之它的 Detail（多维下钻）在此处没有对应物——本项目的「深入」是对同一元件多查一层源，不是换维度切数据。

**同时要简化的**：Amazon 依赖成熟数据基建（Step Functions / EventBridge / MQ）。个人项目没有这些，全部退化为单进程内的 Supervisor + PostgreSQL 状态机 + Redis 协调。**不引入消息队列、不引入工作流引擎。**

---

## 4. 被否决的方案（不要重走）

| 方案 | 否决理由 |
|---|---|
| 定时巡检驱动 | 个人项目无常驻基建；cron 不体现任何 Agent 能力；成本压在最不确定的外部接口上。已改为事件驱动，见 D01 |
| 把主线窄化为「官方资料核验」 | 砍掉了库存、比价、告警等用户明确要求的能力。主线须覆盖六项能力全集 |
| 照搬 Amazon 五角色 | 见 §3.3 |
| 用 Craft Agents 的 BaseAgent 循环 | 见 §3.1 |
| 第二个 OpenAI 兼容端点充当「多后端」 | 协议相同，证明不了 provider-agnostic。见 Q-02 |
| 以「脏数据处理」作为 agent 存在理由 | 用户明确否决：应从多 agent 架构如何适配应用开发需要出发，而非从数据质量出发 |
| 结构化裁决字段替代 research 报告 | 用户选择保留 research 形态（长链路、可中断恢复） |

---

## 5. 开工前的四个阻塞项

按严重度排序。**前三项不收，F12/F15 无法开工。**

1. **契约缺三张表** —— `artifact` / `context_bundle` / `node_checkpoint`。`ARCHITECTURE.md` 的核心运行时类型与依赖方向都要求它们，`docs/spec/data-model.md` 一张都没定义。已在该文 §11 登记为缺口。

2. **`ARCHITECTURE.md §3` 两个模块抢同一目录** —— `Intent Router` 与 `Supervisor` 都写 `src/supervisor/`。Intent Router 是旧架构遗留，职责已被 Supervisor 吸收，应删除该行。

3. **`ARCHITECTURE.md §5 依赖方向` 整节是旧架构** —— 全节零次提及 Supervisor / Worker / Exception Router，写的仍是 `UI → API → Intent Router → Run Manager`，与 §1 §2 的图直接冲突。

4. **JSONL 会话恢复无落点**（矩阵第 5 行）—— 当前恢复机制是 PostgreSQL node checkpoint。JSONL 会话持久化是 Craft Agents 的特性，用户点名要求覆盖。需决定：是新增一层 JSONL 会话日志（与 checkpoint 并存，服务于「会话级」而非「节点级」恢复），还是判定 checkpoint 已充分覆盖该技术点。**这是设计决策，须用户裁决，不得自行假设。**

---

## 6. 建议开发顺序

依赖顺序即开发顺序。前四项零外部依赖，①②③ 三层可在本地跑完，不被 Q-01/Q-02 阻塞。

```
收阻塞项 1–3
  → F10 ModelBackend + ReplayBackend      多后端契约与能力协商
  → F12 证据层迁移                          证据、定位块、取代链
  → F13 ToolRegistry                       工具四态、权限、幂等
  → F14 ContextCompiler                    manifest、五值排除原因
  → F15 DurableWorkflow                    状态机、checkpoint、恢复
  → F16 Human Gate                         等待即一等状态、审批绑定哈希
  → F22/F23 Supervisor + Exception Router  条件路由、快路径兜底
  → F20 Replay/Eval                        数据集、失败用例、回归门禁
```

---

## 7. 新会话的第一条指令

建议照此开场，避免重复探索：

> 读 `docs/HANDOFF.md` 全文，再读 `PROGRESS.md` 与 `docs/features.json` 确认当前状态。
> 然后处理 HANDOFF §5 的阻塞项 1–3（契约补三张表、删除 Intent Router 行、重写 §5 依赖方向），
> 阻塞项 4 先问我。不要写 Harness 代码，直到这四项收完。

**硬约束**：`docs/features.json` 的 `state` 与 `evidence` 只能由用户授权后流转，Agent 不得自行标记 passing。
