# 交接：多 Agent 架构搭建

> **类型**：交接层 · **读取**：新会话第一条，全文读
> **用完即删**。结论沉淀进正式文档，不要让本文成为第二份长期文档。
> **不收录**：任何已有 SSOT 的正文。技术清单与需求见 `docs/product/requirements.md`，架构见 `ARCHITECTURE.md`，决策见 `DECISIONS.md`，队列见 `docs/features.json`，未决项见 `docs/OPEN-QUESTIONS.md`，状态见 `PROGRESS.md`

---

## 1. 这个项目是什么

**求职用的技术展示项目**，不是要解决真实业务问题的产品。

思路是**先定技术展示清单，再倒推场景**。产品形态（元件采购决策助手）服务于技术点，不是反过来。业务价值与技术覆盖冲突时以技术覆盖优先。

技术清单是 `requirements.md` §2 的 **T01–T12**，那一章是全项目的锚点。**任何设计改动先对着它自查：会不会让某一行退化为「演示不出来」。**

---

## 2. 用户怎么工作（重要，照做能省很多来回）

- **小步走。**一次一块代码，写完交给用户审，审过再写下一块。不要一口气交一个 Feature。
- **测试在审核之后写**，不是同时写。
- **不要长篇方案。**用户明确说过「你写这些都太浮躁」「我都看不懂」。给判断、给代码、给可执行的命令，不要铺陈。
- **先问再花钱。**真实模型调用、外部 API 调用，事前要用户明确授权。
- **用户会推翻既有文档。**说「这条改掉」就改，不要拿旧文档当挡箭牌。
- **数据不许编。**需要示例数据时先说清哪些是真的、哪些是造的，造的要标 `provenance=sample`。

---

## 3. 当前进度

```
工作树：只有 src/contracts/worker.py 未提交（第 1 块，已审过）
测试：make test 198 passed / make test-integration 27 passed
最近提交：976bb17 接通三家真实分销商 API
```

**已建成并验证的能力**（不看 Feature 编号，看实际能跑的东西）：

| 能力 | 落点 |
|---|---|
| 确定性采购核：BOM 展开、缺口、MOQ、阶梯价、版本化方案 | `src/procurement_core/` |
| 统一 `ModelBackend` 端口：能力协商、云端 GLM、回放后端、录制 | `src/contracts/llm.py`、`src/infrastructure/llm.py`、`replay.py` |
| 证据账本（字段型）：只插入、内容去重、取代链、列级 GRANT | `src/persistence/evidence.py` |
| ToolRegistry：按场景注入、重试收口、幂等分级、审计落库 | `src/tools/registry.py` |
| 分层权限 + `make explain` | `src/permissions/` |
| MCP 子进程 + 环境变量白名单 + **三家真实分销商 API** | `mcp_servers/supplier/`、`src/tools/mcp_client.py` |
| 对话入口与流式呈现，右栏按类型渲染 | `src/api/routers/chat.py`、`frontend/` |

**没建的**：Graph 编排器、Worker 本体、Agent、RiskEvent 流、trace、预算、eval、语义层。

技术清单上 **T01 多 Agent 架构 / T02 多 Agent 通信 / T03 并行与依赖** 三条一个都没做——那正是当前任务。

---

## 4. 当前任务：多 Agent 架构搭建

用户否决了「先做 F15 Graph 编排器」的提议，要求**先把 Worker/Agent 的骨架搭起来**，逐块交付审核。

### 已完成的第 1 块

`src/contracts/worker.py`（135 行，未提交）—— Worker 之间传什么。

用户已审过，四个关键判断：

1. **`content` 是 dataclass 不是字符串**。自由文本只在 `narrative` 一个字段，写死「给人看，不给 worker 用，不进控制条件」（BR-20）
2. **`inputs` 放引用不放数据**。Worker 自己用被允许的工具去取，否则绕过权限检查（T04）
3. **`purpose` 是冻结词表**，枚举可路由可计数可评测
4. **`Budget.child(fraction)` 从父预算切**，接口位先留着，F29 才做扣减

`WorkerResult.usable` 把 `partial` 算可用、`error` 算不可用——这条会影响 fan-in 全部逻辑，用户未提异议。

### 第 2 块（下一步做的）：`src/workers/base.py`

用户问「worker 可被继承为单 agent 节点、单工具，还有什么」，重想后的结论已交付但**尚未写成代码**：

**是子类的三种**

| | 差在哪 |
|---|---|
| `AgentWorker` | 有自己的 prompt、工具集、模型档位，跑受限工具循环 |
| `ServiceWorker` | 纯确定性代码，不碰模型 |
| `ToolWorker` | 单工具的机械包装。**参数化一个类即可，不需要写子类** |

**不该是子类的三种**

| | 为什么 |
|---|---|
| Human Gate | 「等待不是错误」是不变量。等待是 **Run 的状态**不是 worker 的返回值；它是图里的**节点类型**，由执行器特殊处理。做成 worker 会逼着在四态里加第五态 |
| Replay / Recording | 是**装饰器**，包住任意 worker，与已有的 `RecordingBackend`/`ReplayBackend` 同一手法 |
| 子图 / Composite | 就是一个调用执行器的 `ServiceWorker`，递归靠「worker 包装成工具」达成 |

**基类最容易踩的坑：基类不能假设有模型。**六个 worker 有三个是确定性服务，基类一旦假设有 prompt 循环，那三个就被迫继承用不上的东西。

基类只负责**信封**：预算守卫、计时、异常→四态、证据引用汇总。**怎么产出内容是子类的事。**

### 之后的块（未定稿，按需调整）

3. 第一个真 agent。建议 `evidence_check`——它的输入（三家真实报价与分歧）现在就有真数据
4. Supervisor 分派与 `RoutingDecision` 留痕
5. 然后才是 F15 Graph 编排器

---

## 5. 真实数据现状（这批是真的，不是造的）

三家分销商 API 全部接通，凭据在 `.env` 的 `SUPPLYAGENT_SUPPLIER_*`：

```bash
make probe-suppliers MPN=IRFZ44NPBF     # ④ 授权端到端，会消耗配额
```

实测返回（同一颗料）：

| 分销商 | 库存 | 交期 | 单价 | 生命周期 |
|---|---|---|---|---|
| DigiKey | 6484 | 224 天 | $1.44 | Active |
| Mouser | 0 | 224 天 | ¥10.54 | — |
| element14 | 2802 | 337 天 | £1.32 | STOCKED |

**三家三种币种**，`prices_comparable=false`，已记为 **Q-09**。

**库存/在途/占用是造的**（`make seed-supply`，固定种子，全标 `is_simulated`）。分销商 SKU 映射是真的（来自 BOM enrichment）。

---

## 6. 容易踩的坑

| 坑 | 说明 |
|---|---|
| **改错 `.env`** | 用户两次把 key 填进了 `.env.example`（模板，进 git）。程序读的是 `.env`。发现 key「没生效」先查这个 |
| **`~/Downloads/AGENTS.md`** | 仓库**外**那份是旧的，写着「尚无产品代码」和旧五角色划分，与仓库内的直接冲突。别读它 |
| **DigiKey sandbox** | `sandbox-api.digikey.com` 前面挂 Cloudflare，对普通客户端一律 403 挑战页。用生产主机，只读查询 |
| **单测不许联网** | `tests/test_sourcing.py` 用 `OFFLINE` spec 清空凭据强制走录音。真实调用有专用命令 |
| **`node_checkpoint` 表没迁移** | `data-model.md` §10 有契约，迁移只到 `0003`（evidence）。F15 要加 `0004` |
| **Feature 状态** | Agent 不得自行改 `docs/features.json` 的 `state` / `evidence`，只能提交验证请求 |

---

## 7. 新会话第一条指令建议

> 读 `docs/HANDOFF.md` 全文，再读 `PROGRESS.md` 确认状态。
> 然后按 §4 继续多 Agent 架构：写 `src/workers/base.py`（第 2 块），
> 交给我审核后再写测试。小步走，一次一块。
