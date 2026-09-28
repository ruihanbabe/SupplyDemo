# 交接：方向重定为「可替代元件检索 + 下单」，数据地基已铺

> **类型**：交接层 · **读取**：新会话第一条，全文读
> **用完即删**。结论沉淀进正式文档，不要让本文成为第二份长期文档。
> **注意**：本文记录的新方向**尚未写进** `requirements.md` / `ARCHITECTURE.md` / `DECISIONS.md`。那三份仍描述旧切片（PCB-Stimulator 缺料采购）。两者冲突时以本文 §2 用户需求为准，并先请用户确认再改正式文档。

---

## 1. 这个项目是什么

**求职用的技术展示项目**。先定技术清单，再倒推场景；业务价值与技术覆盖冲突时技术覆盖优先。技术清单锚点仍是 `requirements.md` §2 的 T01–T12。

---

## 2. 用户的需求（原话要点，最重要）

### 2.1 项目档次：25+ 点

用户给的分档标准：

| 档次 | 点数 | 特征 |
|---|---|---|
| 普通 Agent Demo | 5–8 | LLM + Prompt + Tool Calling + 简单 UI |
| 合格 Agent 项目 | 10–15 | Session、Streaming、Tool、Context、Persistence、错误处理 |
| 较强 Agent 工程项目 | 15–25 | Harness、Runtime Adapter、Capability Layer、Skill、Eval、Trace、Provider fallback |
| **目标：适合 Agent 岗位简历** | **25+** | 不是「Agent 功能」，而是一套完整的 Agent Engineering System |

**现状约 19 点**（有代码、有测试、实际跑过的）：LLM 接入、Tool Calling、Streaming、UI、持久化、错误四态、ModelBackend 端口（Runtime Adapter）、能力协商（Capability Layer）、录制回放、ToolRegistry、MCP、真实外部 API、分层权限 + explain、证据账本、外部内容隔离、确定性核与模型分离、配置化 agent、测试体系、开发 Harness。

**缺口**：多 Agent 编排（fan-out / fan-in / 部分失败）、Trace（树状）、Eval（回归门禁）、Provider fallback（只接了一家模型）、服务端 Session、Context 管理、进程内 Skill、Human Gate、预算。

### 2.2 业务范围（用户已收窄，按此执行）

1. **业务闭环 + 后端写入 + 多渠道推送**：发现问题 → 调后端系统完成闭环操作（如生成采购单）；消息不止一个渠道
2. **知识库的管理与更新**：元件文档信息 + 原始图纸 / BOM 管理，支持更新
3. **业务规则 / 优先级集成进 agent 架构**，要有**明确的整合策略**
4. **多 agent 架构**：单 agent 与整体架构合起来覆盖 25+ 点

### 2.3 场景（用户选定）

**制造业：可替代元件的检索 + 寻找 + 下单**
- **供应商分级**作为业务知识融入开发
- **知识库** = 元件文档信息 + 原始图纸 BOM 管理

用户明确**不要**：
- 旧的大范围目标（多渠道全覆盖、业务人员拖拽改流程等），已被用户自己砍掉
- 继续做传统「缺料采购」主线（原因：供应链数据与 ERP 接入太难）

---

## 3. 用户怎么工作（照做能省很多来回）

- **不要擅自扩大规模。** 用户原话：「你莫名其妙把项目规模做很大，很多数据我都不需要」。只做当前需要的最小一块；多出来的想法先问，不先做。
- **小步走**，一次一块，交给用户审，审过再写下一块；测试在审核之后写。
- **不要长篇方案**，给判断、代码、可执行命令。
- **先问再花钱**：真实模型调用、分销商 API（消耗配额）事前要授权。公开 GitHub 只读拉取不花钱。
- **数据不许编**；造的数据标 `provenance=sample` / `is_simulated`。
- **原始数据原样保留**：不在原始层做统一字段；统一字段在后续加工阶段按「相近字段 + 业务功能」确定。
- **有问题的数据不要偷偷修**：可作为后续测试 / 评测用例的原型。
- 用户要求返回代码时常要「逐句注释」，测试只需简单说明做了什么。
- 用户会推翻既有文档，说改就改。

---

## 4. 数据来源

### 4.1 硬件设计（知识库，真实，公开）

三个开源硬件项目，KiCad 原理图，按提交号钉死，登记在 `config/knowledge/sources.yaml`：

| project_id | 仓库 | 提交 | 许可证 | 特点 |
|---|---|---|---|---|
| `hackrf-one` | greatscottgadgets/hackrf（`hardware/hackrf-one`） | `7f96cc8e3f` | GPL-2.0 | KiCad 6 格式；**设计者在 `Substitution` 字段声明了替代料**（35 个料号），可作 eval 真实标注 |
| `jetson-agx-thor-baseboard` | antmicro/jetson-agx-thor-baseboard | `d966e9ca40` | Apache-2.0 | KiCad 9；最大（1170 元件）；参数字段结构化（Voltage / Dielectric / Tolerance） |
| `bms-c1` | LibreSolar/bms-c1（`kicad/`） | `0ca09706f4` | CERN-OHL-W-2.0 | KiCad 9；每个元件带**立创（LCSC）编号**，天然多渠道 |

选型过程：深度扫描过 6 个仓库（另 3 个 Cynthion、Jetson Orin、Jetson Nano 未选），是凭经验挑的，**不是系统检索**。

### 4.2 分销商报价（真实，已接通）

DigiKey / Mouser / element14 三家 API，凭据在 `.env` 的 `SUPPLYAGENT_SUPPLIER_*`，经 MCP 子进程调用。能返回：库存、交期、阶梯价、生命周期、datasheet 链接、Mouser 的推荐替代（`SuggestedReplacement`，已存在 raw 里未使用）、MOQ / 倍数 / 标准包装量。三家三种币种，不可直接比价（Q-09 未冻结，当前对策：原币分列）。

### 4.3 模拟数据（造的，已标注）

内部库存 / 在途 / 占用：`make seed-supply`，目前钉在**旧切片 PCB-Stimulator**，全标 `is_simulated`。新方向若需要内部库存，要重新决定造哪些。

### 4.4 旧数据（待定去留）

`data/supplychain/`（5 个旧开源项目的 BOM，domdata 只读）及其表，是旧主线的数据。新方向下大概率下线，**未经用户确认不要删**。

---

## 5. 处理进度

### 5.1 已提交（`69e0576`）

| 能力 | 落点 |
|---|---|
| Worker 基类（只含机制）、`AgentWorker` 两段式工具循环 | `src/workers/base.py`、`agent.py` |
| 三个 service worker：`shortage`、`internal`、`sourcing`（旧切片语义） | `src/workers/*/worker.py` |
| service worker 权限配置、`inventory.read` 权限 | `config/agents/*.yaml`、`config/permissions.yaml` |
| 硬件原始层：钉提交拉取（逐文件比对 git blob 哈希、只读、MANIFEST） | `src/knowledge/fetch.py`、`data/hardware/raw/`、`make fetch-hardware` |
| KiCad 解析（自研 S 表达式，兼容 KiCad 6 / 9 实例表） | `src/knowledge/kicad.py` |
| 原始层入库（迁移 0004）：文件全文、图纸页、符号，原属性名不改、不合并、不过滤，只插入 | `src/persistence/import_hardware_raw.py`、`make import-hardware` |

提交时验证：`make lint` 通过；`make test` 258 passed；`make test-integration` 34 passed。

### 5.2 未提交（本会话最后一块，**测试未跑**）

用户选「方案一」：撤回过度设计的加工层，只留**一张料号表**。

| 改动 | 状态 |
|---|---|
| 新表 `hw_part`（迁移 0005）：每项目每个要采购的料号一行 — 料号、厂商、描述、单板用量、设计者替代料原文 | 已写；本地库已迁移并生成（hackrf 68 / thor 115 / bms-c1 62，共 245 行），重跑结果一致 |
| `src/knowledge/parts.py`：只收在 BOM 内、有料号、要贴装的；同一料号两个厂商直接报错不猜 | 已写 |
| `src/persistence/build_hardware_parts.py`、`make build-parts`：从原始层**表**生成，按快照整体替换 | 已写，已实际运行 |
| `kicad.py` 新增 `placements_from_raw`（文件与数据库共用） | 已写 |
| 删除旧的 `src/knowledge/bom.py` 与 `data/hardware/normalized/` CSV 样稿 | 已 `git rm` |
| `data-model.md` §14、`tests/test_schema.py` 表数（契约 40 / 迁移 29）、`test_kicad_ingest.py` 改测料号表、`test_hardware_raw_import.py` 新增料号表集成测试 | 已写 |

**下一会话第一件事**：跑 `make lint`、`make test`、`make test-integration`，全绿后请用户确认再提交。用户在最后一次执行测试前中断了会话，这批改动**一次都没验证过**。

### 5.3 已发现的数据问题（问题原型，代码已撤回，只留记录）

曾用 13 条规则在三个项目里查出 44 条问题（3 条是同一跨项目问题登记三次）。**规则代码已按用户要求撤回**，以下作为以后测试 / 评测用例的原型：

| 问题 | 真实例子 | 处理思路 |
|---|---|---|
| **料号与值冲突**（最重要） | HackRF `RMCF0402JT470R` 是 470Ω 5%，但 R25、R26 的值写 `475` | 不能自动改；agent 取证 + 人工确认，确认前不采购 |
| 替代料尺寸不同 | HackRF 电感 `NRS4018T4R7MDGJ` → 替代 `NRG4026T4R7M`，按命名推测高度 1.8mm 对 2.6mm（**未经 datasheet 核实**） | `spec_check` 的天然测试素材 |
| 替代料没写厂商 | `MAX2837ETM+`、`MAX5864ETM+`、`NRG4026T4R7M` | 用分销商接口按料号查，带证据补，不猜 |
| 包装变体 | `MAX2837ETM+T` ↔ `MAX2837ETM+`（T = 卷带） | 业务规则：工程等价、采购按包装量重算（T12 MOQ/MPQ/SPQ） |
| 值字段写功能名 | LED 写 `TXLED`、接头写 `ANTENNA` | 按料号 + 描述识别物料，不用值 |
| 值字段写 `DNP` 表示不贴装 | HackRF C155、L1、L4、L6、L8、L9、R35 | 已在 `parts.py` 识别为不贴装 |
| 库模板占位值 | Thor 的 `template_dielectric` ×13、`template_tolerance` ×6 | 当未知，参数另从分销商 / datasheet 补 |
| 厂商写法不一 | `YAGEO`（Thor）/ `Yageo`（另两个） | 以后做厂商别名表 |
| 焊线焊盘无料号 | LibreSolar P5（`Connector_Wire:SolderWirePad`） | 不是可采购件 |
| 同料号不同写法 | 旧数据 PCB-Stimulator 的 RS Pro `707-7647` / `7077647` | 规范化时合并（旧数据，可能不再用） |

---

## 6. 新方向的待办与未决（都需用户拍板）

1. **下单后端**：建议本地 Docker 部署 Odoo（开源 ERP）作真实写入对象，**未核实**免费版与 API 现状（上次联网核实时连接中断）
2. **供应商分级**：维度（是否原厂授权、交期稳定性、价格、历史表现——后者只能模拟）与整合策略草案见下；未定稿
   - 草案：`business_rule` 表 → 每个 Run 编译成版本化 PolicySnapshot → 权限层 / 编排器 / 确定性排序 / 审批级别 / RiskEvent / agent 上下文（只注入等级标签）共读；模型不参与定级
3. **datasheet 知识库**：链接来自分销商接口；更新靠文档修订号比对；**尚未做**
4. **分销商扫描**：用三家 API 查这批料号的库存、生命周期、推荐替代，约 245 料号 × 3 家，**消耗配额，需授权**；顺带核实 HackRF 的 `MAX2837` 是否停产（印象中新版硬件换成 `MAX2839`，未核实）
5. **正式文档改写**：requirements 产品形态 / 切片 / 技术清单对照、ARCHITECTURE 名册与流程、DECISIONS（新方向应新增一条决策）——**等用户确认方向后再改**
6. **Worker 名册调整**：新方向建议 `alternates`（agent，找替代料）、`spec_check`（agent，比对参数）、`evidence_check`、`proposal`、`order`（service，写 ERP）；现有 `shortage` / `internal` 语义属旧切片

曾调研并**放弃**的场景（避免重复讨论）：依赖漏洞修复（NVIDIA 开源参考实现与 GitHub Dependabot 指派 AI agent 已占位）、模型许可证审计（用户认为缺少明确业务约束与数据变动）、Wikidata 维护、论文引用核查、法规变更、OSM。

---

## 7. 容易踩的坑

| 坑 | 说明 |
|---|---|
| 改错 `.env` | 程序读 `.env`，不是 `.env.example`（模板，进 git） |
| `~/Downloads/AGENTS.md` | 仓库外的旧文件，与仓库内冲突，别读 |
| DigiKey sandbox | 挂 Cloudflare，一律 403；用生产主机只读 |
| 单测不许联网 | 真实调用有专用命令（`make probe-suppliers`） |
| GitHub 匿名接口 | 每小时 60 次；大文件下载会被截断但 curl 仍返回成功，`fetch.py` 已靠 git blob 哈希重试兜底 |
| KiCad 两代格式 | 6 用根文件 `symbol_instances`，7+ 每个符号自带 `instances`；位号一律取实例表 |
| 测试里的 `now()` | PostgreSQL `now()` 是事务开始时刻，整个测试同一事务，Python 写入的规则会「尚未生效」；测试里把生效时间前移 |
| 本地服务 | 先 `colima start`，再 `make services-up` |
| Feature 状态 | 不得自行改 `docs/features.json` 的 `state` / `evidence` |

---

## 8. 新会话第一条指令建议

> 读 `docs/HANDOFF.md` 全文，再读 `PROGRESS.md`。
> 先跑 `make lint`、`make test`、`make test-integration` 验证 §5.2 未提交的料号表改动，把结果报给我，别急着提交。
> 之后按 §6 逐项问我，一次一件，别扩大范围。
