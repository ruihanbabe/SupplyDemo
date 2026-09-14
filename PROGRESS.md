# SupplyAgent 当前进度

> **文档契约** · 类型：状态层 · 读取：每次会话启动必读，全文
> 更新：**Feature 完成、遇到阻塞、验证失败或阻塞解除后，由 Codex 即时写入**；不必等会话结束
> 独占：当前状态、Git 检查点、环境与文档进度、验证记录、任务看板、阻塞清单、下一步
> 不收录：设计决策与理由（见 `DECISIONS.md`）、架构事实（见 `ARCHITECTURE.md`）、Feature 清单正文（见 `docs/features.json`）、排查过程细节（归 Git commit）

维护规则：本文件只反映当前状态与下一步，每次更新即覆盖旧状态，不追加历史记录。变更过程见 Git commit history 与 `DECISIONS.md`（架构决策）。下次会话开始先读本文件，不必重新翻阅整个仓库或对话记录。

## 当前状态

- 阶段：需求/架构设计阶段。尚无产品代码；当前工作是补全需求/架构文档、验证借用服务器的环境可用性，为进入编码阶段做准备。
- 工作树：文档协作阶段，无未提交的代码改动需要保护。
- 语言/框架：已锁定 Python 3.11+（见 `DECISIONS.md`、`ARCHITECTURE.md`）。

## Git 检查点

- 仓库尚未初始化（无 `.git`）。首次初始化时应同时建立 `.gitignore`（至少排除 `.env`、凭据、本地运行数据、`__pycache__/`）。
- 初始化后：当前检查点、领先/落后远程情况、完整历史均以 `git log --oneline --decorate` 为准，不在本文件复制 commit 历史。

## 环境状态

| 项目 | 状态 | 说明 |
|---|---|---|
| 服务器（借用此前项目的服务器） | 部分就绪，待验证 | 基础环境据称已装七七八八，具体服务（Docker、PostgreSQL、Redis、网络出口等）可用性尚未实际验证 |
| 本地开发机（2020 MBP） | 可用 | 用于编辑与浏览，不承担主要运行负载 |
| 依赖与锁文件 | 未创建 | Python 3.11+ 已锁定；`requirements`/锁文件、`Makefile`、`.env.example`、`compose.yaml` 均待建 |

## 文档体系进度

| 文档 | 状态 |
|---|---|
| `README.md` | 已建 |
| `AGENTS.md`（Codex 入口与硬约束） | 已建 |
| `ARCHITECTURE.md` | 已建（首版，模块划分完成，待随实现细化） |
| `DECISIONS.md` | 已建（D01～D11） |
| `DEVELOPMENT.md`（开发命令、验证层级、Feature 编排契约） | 已建（首版，命令入口待 `Makefile` 落地后替换为真实命令） |
| `CODING_RULES.md`（Codex 文档记录规则） | 已建 |
| `src/*/AGENTS.md`、`src/*/ARCHITECTURE.md`（模块级占位 stub） | 已建（占位，细化随代码进行） |
| `docs/features.json`（Feature 清单 + 三层验证契约） | 待建（机制见 `DEVELOPMENT.md`；建立前用下方任务看板过渡） |
| `productinfo.md`（产品需求，整合版） | 已建（§12/§14/§15 归位待处理，见 T10） |
| `docs/product/GLOSSARY.md`（领域术语表） | 待建（T06） |
| `docs/product/acceptance-cases.md`（EV 验收用例） | 待建（T09） |
| `data/supplychain/normalized/projects.yaml`（项目注册表与已知缺口） | 已建 |

## 验证记录

| 时间 | 命令或范围 | 结果 | 边界 | 失败原因 | 修复动作 |
|---|---|---|---|---|---|
| 2026-09-09 | `python3 normalize_domdata.py` | 通过 | 仅证明原始 CSV 可解析并产出三张长表；未验证数据业务正确性 | — | — |
| 2026-09-09 | 落库结果反查（无 MPN 行、多候选行分布） | 通过 | 仅结构性核对，未做元件身份核验（见 T12） | — | — |
| 2026-09-09 | 对照 Spikeling-V2 项目 README 核验 3 处源表数量/位号异常 | 通过 | 仅核验该项目 3 行；其余项目未逐行对照上游文档 | — | — |

新增记录必须同时填写「失败原因」与「修复动作」两列；`阻塞`/`失败` 结果不得留空这两列。跨 Feature 的自动修复达到单级 3 次上限后，在此表标注升级报告位置，不得继续自行重试（结构化上报格式待设计，见 `AGENTS.md`）。

## 已实现

- **数据层落库**：`data/supplychain/domdata/` 原始层设为只读（`chmod 444`）并生成 `MANIFEST.json`（SHA-256 / 字节数 / 行数）；`normalize_domdata.py` 单向产出规范化层 `data/supplychain/normalized/`，含 `component`(142) / `bom_line`(121) / `bom_line_candidate`(148) / `bom_line_distributor_sku`(160) 四张长表与项目注册表 `projects.yaml`。Spikeling-V2 的 3 处源表笔误已依据项目 README 订正（位号缺前缀、两处数量少计），订正在 `normalize_domdata.py` 的 `CORRECTIONS` 中逐条声明并附依据，原始层未改。宽表中重复的 `Manufacturer,MPN` 列组已按列位置展开，未被解析器静默丢弃。

## 部分实现

- 无。

## 未实现

- 全部代码模块（API / Supervisor / 各 Worker / 采购业务核 / Monitor / Runtime 契约层 / Tools / Persistence / Infrastructure）。
- `Makefile`、依赖锁文件、`.env.example`、`compose.yaml`、`docs/features.json`。
- 合成时间序列数据（库存/出入库/在途事件）——已落库的 BOM 数据只有身份与用量，不含时间序列，不足以支撑库存周转率/缺货率计算（见 `DECISIONS.md` D11）。
- 语义层 / schema 暴露准则（见 `DECISIONS.md` D09，暂缓）。

## 任务看板

| ID | 任务 | 状态 | 阻塞原因 |
|---|---|---|---|
| T01 | 验证借用服务器的服务可用性（Docker / PostgreSQL / Redis 能否正常起停） | 阻塞 | 需用户本人登录验证，无法代为操作 |
| T02 | 建 `AGENTS.md`，给 Codex 的入口与硬约束 | 已完成 | - |
| T03 | 建 `DEVELOPMENT.md` 代码规范（分层、契约层、测试、Git、Lint、Feature 编排） | 进行中 | 首版已建，待 `Makefile` 落地后替换命令入口 |
| T04 | 生成合成时间序列数据（库存/出入库/物流事件） | 未开始 | BOM 侧 schema 已定稿（见 `normalized/`），可开始设计时间序列 schema |
| T12 | 元件身份核验：厂商别名、无 MPN 行、多候选行的技术等价性 | 未开始 | 3 行无 MPN；18 行多候选。清单见 `normalized/projects.yaml` |
| T05 | 补充语义层 / schema 暴露准则设计 | 阻塞 | 缺业务背景支撑（见 `DECISIONS.md` D09），需先有真实提问样本再反推 |
| T06 | 建 `docs/product/GLOSSARY.md` 领域术语表（BOM / MPN / reference / candidate / distributor_sku / logical_action_id 等） | 未开始 | 术语现散落在归档笔记散文中，多 Agent 协作前需统一 |
| T07 | 建 `docs/features.json` 初始清单并接入编排 Harness | 未开始 | 依赖 T09 与代码骨架 |
| T08 | 初始化 Git 仓库 + `.gitignore` | 未开始 | - |
| T09 | 把 FR-01～10 / BR-01～11 / EV-01～28 从归档目录提升为正式需求与验收章节 | 挂起（用户决定本轮不处理） | 正文现仅存于 `data/supplychain/MVP-PRD.md`、`ACCEPTANCE-EVAL.md`，两者题头均标注为初稿，却被当作权威引用 |
| T10 | `productinfo.md` 的 §12 / §14 / §15 归位与标题占位修正 | 挂起（用户决定本轮不处理） | 详见该文件题头契约块的「待办」行 |
| T11 | `data/supplychain/` 归档标记与 `DECISIONS.md` 头部「讨论细节以工作笔记为准」的处置 | 挂起（待用户带回决定） | - |

## 当前阻塞清单

- **T01**：服务器服务可用性未验证，不确定能否正常跑 Docker Compose（PostgreSQL + Redis），需要用户本人登录确认。
- **T05**：语义层设计缺业务背景支撑，暂缓，等实际场景/数据跑起来后再回来做。

## 交接下一步

1. 验证服务器环境（T01）——当前唯一的硬阻塞，验证完才好评估是否现在开始搭 Docker Compose。
2. 设计合成时间序列数据 schema（T04），BOM 侧已落库可作为锚点。
3. 建领域术语表（T06），统一 BOM / MPN / reference 等词汇，避免多 Agent 各自发明字段名。
4. 建 `docs/features.json` 初始清单（T07），按 `DEVELOPMENT.md` 的六字段契约与 `depends_on` 拓扑关系拆分。
5. 初始化 Git 仓库与 `.gitignore`（T08），建立首个检查点。

## 最近更新

2026-09-09 · (-1) 数据层收敛：删除 `component-library/`（与 `normalized/` 重复的第二套流水线），元件主表并入 `normalized/component.csv`；厂商别名归一 5 组；新增 D12（MVP 移除 RAG）、D13（供应查询默认单源）。(0) 数据集调整为当前 5 个项目，新增 Spikeling-V2；`domdata/`、`index.csv`、`normalized/`、`normalize_domdata.py` 同步更新。(1) 对齐 LawAgent 同名文档的信息要素：`AGENTS.md` 去占位并补模块入口；`DEVELOPMENT.md`（根目录）由 LawAgent 拷贝适配为 SupplyAgent 版；新建 `src/*/` 模块级占位 stub；本文件补入 Git 检查点 / 验证记录 / 已实现·部分实现·未实现 分块。(2) 建立文档治理体系：全部 33 份 Markdown 加「文档契约」题头块（类型/读取时机/更新时机/独占/不收录）；`CODING_RULES.md` 新增全局文档职责表、SSOT 矩阵、不变量双写分工、「不为实现过程新建文档」规则；修正全部指向不存在的 `docs/product/requirements.md` 的引用为 `productinfo.md`。(3) 数据层落库：`domdata/` 设只读 + `MANIFEST.json`；`normalize_domdata.py` 产出 `normalized/` 三张长表与 `projects.yaml`；全部指向不存在的 `docs/data/` 的引用改为该注册表。

---

任何状态改为「已实现」前，都必须在目标服务器的同一代码版本上重新验证。真实模型调用、远程服务器访问和付费工作必须获得用户明确授权。
