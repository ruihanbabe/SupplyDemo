# SupplyAgent 当前进度

> **文档契约** · 类型：状态层 · 读取：每次会话启动必读，全文
> 更新：**Feature 完成、遇到阻塞、验证失败或阻塞解除后，由 Codex 即时写入**；不必等会话结束
> 独占：当前状态、Git 检查点、环境与文档进度、验证记录、任务看板、阻塞清单、下一步
> 不收录：设计决策与理由（见 `DECISIONS.md`）、架构事实（见 `ARCHITECTURE.md`）、Feature 清单正文（见 `docs/features.json`）、排查过程细节（归 Git commit）

维护规则：本文件只反映当前状态与下一步，每次更新即覆盖旧状态，不追加历史记录。变更过程见 Git commit history 与 `DECISIONS.md`（架构决策）。下次会话开始先读本文件，不必重新翻阅整个仓库或对话记录。

## 当前状态

- 阶段：需求/架构设计阶段，开发环境已就位。尚无产品代码；当前工作是完成需求/验收/spec 文档的解耦与补全，为进入编码阶段做准备。
- 工作树：文档协作阶段，无产品代码改动需要保护；当前是否有未提交内容以 `git status --short --branch` 为准。
- 语言/框架：已锁定 Python 3.11（见 `DECISIONS.md`、`ARCHITECTURE.md`）。
- 开发环境：**本地 MacBook Pro**，借用的 GPU 服务器不再是默认环境（见 `DECISIONS.md` D18）。

## Git 检查点

- 仓库已初始化，分支 `main`，基线 commit `5eb4dbc`（文档解耦前的可回滚快照，53 个文件）。`.gitignore` 已覆盖 `.env`、凭据、`__pycache__/`、`.venv/`、`.runtime/`、`.DS_Store`。
- 当前检查点、领先/落后远程情况、完整历史均以 `git log --oneline --decorate` 为准，不在本文件复制 commit 历史。

## 环境状态

开发在本地进行。组件版本台账见根目录 `requirement.txt`，服务拓扑见 `compose.yaml`，命令入口见 `Makefile`。

| 项目 | 状态 | 说明 |
|---|---|---|
| 本地开发机（2020 MBP，Intel i5 / 32 GB） | **就绪** | Python 3.11.15 + `.venv`；Docker CE 29.7.1（colima + vz）；PostgreSQL 16.6 与 Redis 7.4.2 容器已起。用户本人执行并确认成功，Claude 未复核 |
| 入口文件 | **已建** | `Makefile`、`compose.yaml`、`.env.example` 均已落地并接入文档 |
| 依赖锁文件 | **已生成** | `requirements.lock.txt` 44 条；直接依赖清单与排除项见 `requirement.txt` |
| 借用服务器（带 GPU） | 停用 | 不再是默认开发环境。仅在 ④ 层需要常开宿主跑 ERPNext、或将来重新引入需 GPU 的能力时再启用（见 D18） |

## 文档体系进度

| 文档 | 状态 |
|---|---|
| `README.md` | 已建 |
| `AGENTS.md`（Codex 入口与硬约束） | 已建 |
| `ARCHITECTURE.md` | 已建（首版，模块划分完成，待随实现细化） |
| `DECISIONS.md` | 已建（D01～D18） |
| `DEVELOPMENT.md`（开发命令、验证层级、Feature 编排契约） | 已建；环境与命令节已按真实 `Makefile` 改写。**Feature 三层契约仍待按 D14 改为四层投影** |
| `CODING_RULES.md`（Codex 文档记录规则） | 已建（SSOT 矩阵待补入新增文档） |
| `src/*/AGENTS.md`、`src/*/ARCHITECTURE.md`（模块级占位 stub） | 已建（占位，细化随代码进行） |
| `Makefile` / `compose.yaml` / `.env.example` | **已建**，是命令与拓扑的权威来源 |
| `requirement.txt`（依赖与环境台账） | 已建；按 D17 纳入治理并剔除遗留内容 |
| `docs/features.json`（Feature 清单 + 验证契约） | **已建**（T07）；首切片 7 个 Feature，拓扑 F01→F02→F03→(F04,F05)→F06→F07，静态校验通过 |
| `productinfo.md`（产品需求，整合版） | 已建；§5/§6/§10 已归位，§12/§14/§15 **待归位**（见 T10） |
| `docs/product/requirements.md`（FR/BR 正文） | **已建**（T09 部分完成） |
| `docs/product/acceptance-cases.md`（EV 验收用例） | **已建**（T09 部分完成） |
| `docs/product/GLOSSARY.md`（领域术语表） | **已建**（T06 完成） |
| `docs/spec/interfaces.md` / `state-machine.md` / `data-model.md` | **已建**（T13 完成）；DDL 已在真实 PostgreSQL 16.6 上验证可建，BR 约束实测生效 |
| `docs/research/procurement-platforms.md` | **已建**（T13 完成）；快照日期 2026-09-09，接入前须重新核对 |
| `data/supplychain/` 三份归档初稿 | **已清空为指针**（T14 完成）；各含文档契约块与逐节去向对照表，原文见 `git show 5eb4dbc:<path>` |
| `data/supplychain/normalized/projects.yaml` | 已建；known_gaps 已订正，声明的多候选行数（20）与落库实际一致 |

## 验证记录

| 时间 | 命令或范围 | 结果 | 边界 | 失败原因 | 修复动作 |
|---|---|---|---|---|---|
| 2026-09-09 | `python3 normalize_domdata.py` | 通过 | 仅证明原始 CSV 可解析并产出三张长表；未验证数据业务正确性 | — | — |
| 2026-09-09 | 落库结果反查（无 MPN 行、多候选行分布） | 通过 | 仅结构性核对，未做元件身份核验（见 T12） | — | — |
| 2026-09-09 | 对照 Spikeling-V2 项目 README 核验 3 处源表数量/位号异常 | 通过 | 仅核验该项目 3 行；其余项目未逐行对照上游文档 | — | — |
| 2026-09-14 | 本地装 Docker CE / Compose / colima，起 PostgreSQL 16.6 + Redis 7.4.2 | 通过 | 用户本人执行并确认成功，**Claude 未复核**；仅证明服务能起，未验证应用级接入或 schema | 首次尝试失败三次：brew 停供 Intel 预编译包导致退化为源码编译；curl 无续传导致 docker 包截断；lima 镜像下载被污染致 SHA512 失配 | 改用官方静态二进制绕开 brew；改用 `curl -C -` 续传循环；删除污染文件后从零重下并校验 SHA512 通过 |
| 2026-09-14 | `docs/spec/data-model.md` 的 DDL 在真实 PostgreSQL 16.6 上应用 | 通过 | 22 张表全部建成，9 条 CHECK / 3 条 UNIQUE 生效；BR-06 价币配对、BR-04 重复占用、BR-10 已解决行、BR-11 幂等键、审计表 UPDATE 拒绝 五项实测均正确拒绝违规写入。**仅验证 schema 可建与约束生效，未验证业务逻辑正确性** | — | — |
| 2026-09-14 | `docs/spec/interfaces.md` 的 JSON Schema 解析 | 通过 | 7 个 JSON 块全部解析成功（5 个工具 schema + 2 个示例）；**未用真实供应商响应校准字段** | — | — |
| 2026-09-14 | 全仓 Markdown 引用与契约块扫描 | **发现缺口** | 45 份（已排除 `.venv`）；裸文件名按 basename 解析 | `CODING_RULES.md:89/103` 两处把不存在的 `TASKS.md` 断言为任务验收 SSOT；归档三份文档缺文档契约块 | 前者归 T16，后者归 T14 |
| 2026-09-14 | Codex 冷启动模拟：仅用 `AGENTS.md` 启动流程可达的文档检索关键规则 | 通过 | 42 份可达文档；检索 9 项关键规则（BR-03/04 正文、审批门、approved=true 禁令、幂等键、缺口表、状态迁移、EV 算例）全部命中，反向检查确认无一处依赖归档正文 | — | — |
| 2026-09-14 | 归档清空前的内容覆盖核对 | 通过 | 11 项条款逐条确认落点；**按关键短语检索，非逐字比对** | 初查发现 MVP-PRD §6 界面清单（六处界面 + 内部框架名称不占据用户流程）无落点 | 迁入 `productinfo.md` §11 后再清空 |
| 2026-09-14 | 落库数据反查多候选行数 | **发现偏差** | 仅核对计数，未做元件身份核验 | `projects.yaml` 的 Spikeling-V2 known_gaps 漏记 2 行多候选，实际 20 行而非 18 行 | 待订正 `projects.yaml` 与 T12 描述（见 T12） |

新增记录必须同时填写「失败原因」与「修复动作」两列；`阻塞`/`失败` 结果不得留空这两列。跨 Feature 的自动修复达到单级 3 次上限后，在此表标注升级报告位置，不得继续自行重试（结构化上报格式待设计，见 `AGENTS.md`）。

## 已实现

- **文档解耦（D15 闭环）**：`docs/product/` 三份、`docs/spec/` 三份、`docs/research/` 一份共七份新文档承接全部正文；归档三份清空为指针；全局引用改写完毕。冷启动模拟验证：仅按 `AGENTS.md` 启动流程可达的 42 份文档中，BR-03／BR-04 缺口规则、审批门、幂等键、状态机迁移、EV 算例九项全部可达，且无一处仍依赖归档正文。**`AGENTS.md` 禁止读归档区与需求正文只存于归档区的自相矛盾已解除。**

- **数据层落库**：`data/supplychain/domdata/` 原始层设为只读（`chmod 444`）并生成 `MANIFEST.json`（SHA-256 / 字节数 / 行数）；`normalize_domdata.py` 单向产出规范化层 `data/supplychain/normalized/`，含 `component`(142) / `bom_line`(121) / `bom_line_candidate`(148) / `bom_line_distributor_sku`(160) 四张长表与项目注册表 `projects.yaml`。Spikeling-V2 的 3 处源表笔误已依据项目 README 订正（位号缺前缀、两处数量少计），订正在 `normalize_domdata.py` 的 `CORRECTIONS` 中逐条声明并附依据，原始层未改。宽表中重复的 `Manufacturer,MPN` 列组已按列位置展开，未被解析器静默丢弃。

- **本地开发环境**：Git 仓库初始化（基线 `5eb4dbc`）；`.venv`（Python 3.11.15）+ 直接依赖；Docker CE 29.7.1（colima + vz）；PostgreSQL 16.6 与 Redis 7.4.2 容器。`Makefile`、`compose.yaml`、`.env.example` 三个入口文件已落地，`AGENTS.md` / `DEVELOPMENT.md` / `requirement.txt` 已同步为真实命令与真实版本。

## 部分实现

- **`productinfo.md` 归位（对应 T10）**：§5/§6 已合并归档稿的范围条款并去重，§10 已瘦身为硬门禁一句话加指针，标题占位已改；§12/§14/§15 归位尚未完成。

## 未实现

- 全部代码模块（API / Supervisor / 各 Worker / 采购业务核 / Monitor / Runtime 契约层 / Tools / Persistence / Infrastructure）。
- 全部 Feature（F01～F07 均为 `planned`）；`config/agents/*.yaml`；编排 Harness。
- 合成时间序列数据（库存/出入库/在途事件）——已落库的 BOM 数据只有身份与用量，不含时间序列，不足以支撑库存周转率/缺货率计算（见 `DECISIONS.md` D11）。
- 语义层 / schema 暴露准则（见 `DECISIONS.md` D09，暂缓）。

## 任务看板

| ID | 任务 | 状态 | 阻塞原因 |
|---|---|---|---|
| T01 | ~~验证借用服务器的服务可用性~~ | 已关闭 | 开发环境迁至本地（`DECISIONS.md` D18），该验证不再是前置条件。服务器若将来重新启用，另开任务 |
| T02 | 建 `AGENTS.md`，给 Codex 的入口与硬约束 | 已完成 | - |
| T03 | 建 `DEVELOPMENT.md` 代码规范 | 部分完成 | 环境与命令节已按真实 `Makefile` 改写；Feature 验证契约仍是三层，待按 D14 改为四层投影（见 T15） |
| T08 | 初始化 Git 仓库 + `.gitignore` | 已完成 | 基线 `5eb4dbc` |
| T06 | 建 `docs/product/GLOSSARY.md` 领域术语表 | 已完成 | 字段名以 `normalized/` 实际列名为准 |
| T09 | 把 FR / BR / EV 从归档目录提升为正式需求与验收章节 | 已完成 | - |
| T10 | `productinfo.md` 归位与瘦身 | 已完成 | 15 节压到 12 节；存储表迁 `ARCHITECTURE.md`（删 pgvector 行），开工门槛迁 `PROGRESS.md`，§15 五条各自归位（项目名一条已不成立，删除） |
| T13 | 建 `docs/spec/` 与 `docs/research/procurement-platforms.md` | 已完成 | 四份已建。DDL 实测可建；工具 schema 的**字段级校准仍 blocked**（DigiKey 凭据已丢失，见阻塞清单） |
| T14 | 归档三份初稿正文清空为指针 + 全局引用改写 | 已完成 | 清空前逐条核对 11 项条款均有落点；`AGENTS.md` 归档约束与文档索引一并改写 |
| T15 | `DEVELOPMENT.md` Feature 验证契约按 D14 改为四层投影 | 已完成 | `verification`/`evidence` 四键化；修复级（A/B/C/D）与验证层显式区分 |
| T16 | `CODING_RULES.md` 治理表与 SSOT 矩阵补新文档、删 `TASKS.md` 断言、类型枚举补「参考」 | 已完成 | 职责表补 5 行、SSOT 矩阵补 9 行 |
| T04 | 生成合成时间序列数据（库存/出入库/物流事件） | 未开始 | `docs/spec/data-model.md` 已就位可作锚点。**表结构仍待定**：SKU 数量、时间跨度、异常注入规则未决（原 `productinfo.md` §15-3）；`inventory` 当前是快照非事件流，Monitor 算周转率需事件级历史 |
| T12 | 元件身份核验：厂商别名、无 MPN 行、多候选行的技术等价性 | 未开始 | 3 行无 MPN、20 行多候选，清单见 `projects.yaml`。计数偏差已订正（Spikeling-V2 补记 2 行）；**核验本身未做**——142 个元件仍全部为 `source_asserted` |
| T07 | 建 `docs/features.json` 初始清单并接入编排 Harness | 部分完成 | 清单已建（F01～F07，首切片=采购业务核+Persistence）；**编排 Harness 本身未实现**，过渡期由人工按拓扑顺序推进 |
| T05 | 补充语义层 / schema 暴露准则设计 | 阻塞 | 缺业务背景支撑（见 `DECISIONS.md` D09），需先有真实提问样本再反推 |
| T17 | 跑 `make freeze` 生成 `requirements.lock.txt` | 已完成 | 44 条，全部 pin 解析成功，无排除项混入 |
| T18 | 确定各 Worker 的 Model Provider 具体型号 | 未开始 | 能力需求矩阵已定（`productinfo.md` §12），按 D06 分旗舰/便宜快速两档；**需实测后确定**，调用真实模型前须获用户授权（原 `productinfo.md` §15-5） |

## 当前阻塞清单

- **T05**：语义层设计缺业务背景支撑，暂缓，等实际场景/数据跑起来后再回来做。
- **T13 的部分范围**：`.env.digikey` 凭据已丢失且无备份，需重新申请 DigiKey Client ID/Secret。接口 schema 可按官方文档先写，但无法用实测响应样本校准字段，字段级验证记为 blocked。

无硬阻塞。原唯一硬阻塞 T01 已因开发环境迁至本地而关闭。

## 交接下一步

**文档治理已收尾，已交接 Codex 进入实现阶段。** 首切片为采购业务核 + Persistence（`docs/features.json`），选它because 零外部依赖——不需要 LLM、供应商 API 或 ERP，①②③ 三层全部能在本地跑完。DigiKey 凭据丢失与 T04 时间序列缺失均不阻塞本切片。

1. **F01 项目骨架 + Alembic 建表**——Codex 的第一站。目录须按 `ARCHITECTURE.md`「计划代码落点」创建，不得自行发明模块划分；pytest 需注册 `integration` marker，否则 `make test` 的 `-m` 过滤无效。
2. 按 `docs/features.json` 拓扑顺序推进 F02～F07。编排 Harness 未实现，过渡期人工把关 `state` 与 `evidence`，Agent 不得自行标 `passing`。
3. 设计合成时间序列数据 schema 并生成数据（T04）——Monitor 层（FR-08）没有它无法验证，`inventory` 当前是快照非事件流。
4. 元件身份核验（T12）：3 行无 MPN、20 行多候选，142 个元件仍全部 `source_asserted`。
5. 重新申请 DigiKey 凭据，解除 `docs/spec/interfaces.md` 字段级校准的阻塞；之后才谈得上 ④ 层。

## 最近更新

2026-09-14（下半程）· (6) T15：`DEVELOPMENT.md` 的 Feature 契约按 D14 改为四层投影，`verification`/`evidence` 键名改为 `contract`/`offline`/`integration`/`e2e`；显式区分「验证层」与「修复级 A/B/C/D」两套概念，全仓不再有三层表述。(7) T16：`CODING_RULES.md` 职责表补 5 行、SSOT 矩阵补 9 行，删除把不存在的 `TASKS.md` 断言为任务验收 SSOT 的两处，文档类型枚举补「参考」。(8) T10：`productinfo.md` 由 15 节压到 12 节——存储分工表迁入 `ARCHITECTURE.md` 新增「存储分工」节并删去与 D12 冲突的 pgvector 行，开工门槛迁入本文件末尾并按 D18 改写，§15 五条各自归位（「项目名称/主语言/目录待定」已不成立，删除；Model Provider 选型立为 T18）。(9) T12 计数订正：`projects.yaml` 的 Spikeling-V2 补记 2 行多候选，声明数与落库实际现均为 20 行。

2026-09-14 · (1) 建 Git 仓库与基线 `5eb4dbc`（T08）。(2) 冲突台账裁定落 `DECISIONS.md` D14～D17：验证层级归一到四层、需求正文迁出归档区、FR/BR/EV 编号留空缺不回收、`requirement.txt` 纳入治理。(3) 需求层解耦：新建 `docs/product/requirements.md`（FR/BR 正文）、`acceptance-cases.md`（EV 正文 + 追踪矩阵 + 层级归属）、`GLOSSARY.md`；`productinfo.md` 范围章节合并去重、标题占位修正。(4) 开发环境迁至本地（D18）：`.venv` + Docker CE 29.7.1（colima）+ PostgreSQL 16.6 + Redis 7.4.2；新建 `Makefile` / `compose.yaml` / `.env.example`；`AGENTS.md`、`DEVELOPMENT.md` 的命令与环境节改写为真实入口；`requirement.txt` 由遗留台账适配为 SupplyAgent 版并剔除 LawAgent 依赖。(5) 发现 `projects.yaml` 多候选计数偏差（18→20），待订正。

2026-09-09 · (-1) 数据层收敛：删除 `component-library/`（与 `normalized/` 重复的第二套流水线），元件主表并入 `normalized/component.csv`；厂商别名归一 5 组；新增 D12（MVP 移除 RAG）、D13（供应查询默认单源）。(0) 数据集调整为当前 5 个项目，新增 Spikeling-V2；`domdata/`、`index.csv`、`normalized/`、`normalize_domdata.py` 同步更新。(1) 对齐 LawAgent 同名文档的信息要素：`AGENTS.md` 去占位并补模块入口；`DEVELOPMENT.md`（根目录）由 LawAgent 拷贝适配为 SupplyAgent 版；新建 `src/*/` 模块级占位 stub；本文件补入 Git 检查点 / 验证记录 / 已实现·部分实现·未实现 分块。(2) 建立文档治理体系：全部 33 份 Markdown 加「文档契约」题头块（类型/读取时机/更新时机/独占/不收录）；`CODING_RULES.md` 新增全局文档职责表、SSOT 矩阵、不变量双写分工、「不为实现过程新建文档」规则；修正全部指向不存在的 `docs/product/requirements.md` 的引用为 `productinfo.md`。(3) 数据层落库：`domdata/` 设只读 + `MANIFEST.json`；`normalize_domdata.py` 产出 `normalized/` 三张长表与 `projects.yaml`；全部指向不存在的 `docs/data/` 的引用改为该注册表。

---

**开工门槛**（原 `productinfo.md` §12）：任何未经真实运行验证的实现状态不得标记为已完成。状态改为「已实现」前，必须在本地开发环境的同一代码版本上重新跑通该 Feature 所需的验证层级（见 `DECISIONS.md` D14）；`docs/spec/data-model.md` 的 DDL 与现有数据文件完整性是代码阶段开工的前置。

真实模型调用、外部供应商 API 调用和任何产生费用的操作，必须获得用户明确授权。若将来重新启用远端环境，「本地通过」不自动等于远端通过，须按 D18 约束复测。
