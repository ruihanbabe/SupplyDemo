# SupplyAgent Codex 入口

> **文档契约** · 类型：入口层 · 读取：每次会话启动必读，全文
> 更新：全局硬约束或模块划分变更且用户明确授权时
> 独占：Codex 全局行为硬约束、新会话启动流程、顶层文档与模块入口索引
> 不收录：架构事实、决策理由、需求条款、当前状态、命令实现（各自见索引指向的文档）

面向采购、计划与工程协同人员的元件供应风险主动预警系统。核心是事件驱动的告警（库存告急、停产）与围绕告警的决策支持（多源库存调研、比价建议、官方推荐替代、多候选对比）。项目用确定性业务核、可审计 Agent Harness 与持久化工作流形成证据闭环；已有采购核心、持久化与部分 API 代码，准确状态见 `PROGRESS.md`。

**新会话先读 `docs/HANDOFF.md`**：技术点覆盖矩阵、三个参考来源的借用边界、已被否决的方案、开工前阻塞项。

## 新会话启动

1. 阅读本文件、`README.md`、`PROGRESS.md`；`DECISIONS.md` 只浏览决策标题（`grep '^## ' DECISIONS.md`）建立索引，具体条目按需定点读取，不在启动阶段通读全文。
2. 运行 `git status --short --branch`，保护已有未提交修改。仓库已初始化（`main` 分支）。
3. 运行 `make status` 确认解释器、`.venv` 与 `.env` 就位；需要真实服务时再 `make services-up` + `make services-smoke`。开发环境在本地，不连远程服务器（见 `DECISIONS.md` D16）。
4. 根据任务确定所属模块（参照 `ARCHITECTURE.md` 的一级逻辑模块表），先读该目录的局部 `AGENTS.md`（占位 stub 已建，见下方"模块入口"）；stub 内容较薄时以 `ARCHITECTURE.md` 对应模块行为准。
5. 不默认扫描整个仓库或全部 Markdown；只读取与当前任务有关的文档和模块。

## 标准命令入口

```bash
make help
```

`Makefile` 是命令的唯一权威入口，以 `make help` 实际列出的目标为准。常用：`make setup`（建 `.venv` + 装依赖 + 生成 `.env`）、`make status`（查环境，不启动任何东西）、`make check`（离线门禁）、`make services-up` / `make services-smoke`（起 PostgreSQL + Redis 并冒烟）。

开发在**本地**进行（见 `DECISIONS.md` D16）：Python 3.11.15 / `.venv`、Docker CE 29.7.1（colima）、PostgreSQL 16.6、Redis 7.4.2。服务拓扑见 `compose.yaml`，环境变量模板见 `.env.example`，依赖版本台账见 `requirement.txt`。

`make run` 启动本地 FastAPI，`make health` 检查就绪。命令目标在源码或测试为空时打印「跳过」并以 0 退出；**跳过不等于通过**。Markdown 不能证明当前依赖、服务或外部系统状态，一律以 `Makefile` 与实际运行结果为准。

## 全局硬约束

- Feature 选择与并行边界：完整规则见 `DEVELOPMENT.md`「Feature 选择顺序与并行边界」，Feature 清单为 `docs/features.json`；默认一次只推进一个明确范围的任务，不擅自顺带重构无关模块。
- 只读取和修改当前任务需要的文件，不做相邻重构；保留用户已有改动。
- **业务阈值与供应商特定映射不得硬编码进工作流节点或 prompt**：可变业务参数来自 PostgreSQL 的 `business_rule` 表；供应商字段映射归 Tool/Adapter 层。
- 读取 `DECISIONS.md`、`ARCHITECTURE.md`、`productinfo.md` 时按标题/章节号定点读取，不通读全文：`DECISIONS.md` 各条目以 `## Dxx` 开头，可用 `grep` 定位；`productinfo.md` 采用编号章节（`## N.`），按需只读对应节。仅当怀疑"决策理解有误"且定点读取仍无法确认时，才临时读整份文件排查，排查完不得把全文留在长期上下文。
- 每份文档开头的**文档契约块**（`> **文档契约**` 四行）规定了该文档的读取时机、更新时机、独占信息与不收录信息；写入任何信息前先按该块判断归属，规则见 `CODING_RULES.md`。同一信息只在其 SSOT 文档保留正文，其他文档只写编号引用或一句话指针。
- `data/supplychain/` 下的三份设计初稿为**归档**，正文已按 `DECISIONS.md` D15 迁出，现仅剩指向新位置的对照表。**不读入**——需求条款直接读 `docs/product/requirements.md`，验收用例读 `docs/product/acceptance-cases.md`，接口与数据契约读 `docs/spec/`。同目录的 `domdata/` 与 `normalized/` 是数据层，不受此条约束，按下方数据层规则处理。
- Review 或诊断任务默认只报告，不自动修改。
- 不提交 `.env`、凭据、API Key、未脱敏的供应商报价数据或完整 Provider payload。
- **`data/supplychain/domdata/` 是原始数据层，只读，任何情况下不得修改、清洗、重排或补字段**（文件已设 `chmod 444`，完整性由同目录 `MANIFEST.json` 的 SHA-256 保证）。需要修正数据时改 `normalize_domdata.py` 的转换逻辑并重跑，产出落在 `data/supplychain/normalized/`；发现原始数据本身有误，报告给用户，不自行订正。
- 连接借用服务器、调用真实模型或外部供应商 API、或产生费用前，必须获得用户明确授权。
- 模型不得绕过 Tool Registry/Adapter 层直接调用外部系统；写操作不得绕过 `PermissionDecision` 审批门（见 `DECISIONS.md` D08、D12）。
- 只报告当前环境实际执行成功的验证；mock、语法编译和历史结果不是完整验收。
- 完成以外部验证证据为准，不以自评或"代码写完"代替；必需验证被阻塞时如实报告阻塞原因，并同步更新 `PROGRESS.md` 对应任务。
- 跨模块修改前必须遵守 `ARCHITECTURE.md` 定义的边界、数据所有权和依赖方向；稳定、可客观检测的约束应有自动检查，失败信息需说明何处违约、为何、如何修复。
- 重复、高风险且可客观检测的审查问题应提升为自动检查；不强行自动化纯审美意见。
- 过时历史不留在工作树；仅在用户明确要求时从 Git 历史恢复。
- Feature 的完成定义是 `DECISIONS.md` D14 的四层验证（① 静态契约 ② 离线测试 ③ 集成故障注入 ④ 授权端到端），层序执行、不得跳级；③④ 被阻塞时如实记 `blocked`，不得跳过后宣称完成。自动修复的重试计数是另一套：A 语法/类型 → B lint → C 单元测试 → D 模块/集成，每级上限 3 次、独立计数，达到上限须停止并如实报告。两者区别见 `DEVELOPMENT.md`。修复动作若改变函数签名或接口，须回退重跑更早的验证级别，不由模型自行判断是否需要回退。
- 遇到自身能力/权限不匹配、或验证反复失败超出上限的情况，必须停止并向用户说明阻塞原因，不无限重试、不静默放弃、不自行决定绕过约束；结构化上报机制的具体格式待后续设计。

## 文档维护

Codex 以本套文档作为需求与约束来源。产品、需求、架构和治理文档只在用户明确要求或授权时改写；普通实现任务不得顺带改变产品决策。文档冲突时先停止相关实现并请求用户裁定。

## 顶层文档与模块入口

**需求与验收**

- 产品定位、用户、MVP 范围与非范围、业务边界：`productinfo.md`（按编号章节定点读）
- 功能需求 FR-01～09 与业务规则 BR-01～14 条款正文：`docs/product/requirements.md`（按编号定点读；含模块反向索引，可反查本模块需满足哪些条款）
- 验收用例 EV 与判定原则、FR→EV 追踪矩阵：`docs/product/acceptance-cases.md`
- 领域术语（BOM / MPN / candidate / logical_action_id 等）：`docs/product/GLOSSARY.md`

**接口与数据契约**（实现前必读对应章节）

- 工具 schema、`ToolResult` 统一信封、错误模型：`docs/spec/interfaces.md`
- 任务状态取值、合法迁移矩阵、状态机不变量：`docs/spec/state-machine.md`
- PostgreSQL 表结构与约束、审计表只插入的强制方式：`docs/spec/data-model.md`
- HTTP 响应包络、端点清单、错误映射、留给业务逻辑的接口位：`docs/spec/http-api.md`
- 界面视图、交互映射、不确定性标注规则：`docs/spec/ui-contract.md`

**架构与治理**

- 系统架构、模块边界和依赖方向：`ARCHITECTURE.md`
- 当前设计约束：`DECISIONS.md`
- 当前状态、阻塞和下一步：`PROGRESS.md`
- 开发命令、验证层级与代码规范：`DEVELOPMENT.md`（仓库根目录）
- Codex 文档记录规则：`CODING_RULES.md`
- 依赖与环境台账：`requirement.txt`
- Feature 清单与验证契约：`docs/features.json`（待建，机制见 `DEVELOPMENT.md`；建立前用 `PROGRESS.md` 任务看板过渡）

**参考**

- 采购平台能力与配额、ERP 候选评估：`docs/research/procurement-platforms.md`（快照，接入前须重新核对）

进入以下模块前，先读取其局部 `AGENTS.md`（占位 stub 已建，职责与依赖以 `ARCHITECTURE.md` 一级逻辑模块表为准，细化随代码进行）：

- API（鉴权与路由）：`src/api/AGENTS.md`
- Durable Runtime / Harness：目标目录见 `ARCHITECTURE.md`，创建前先确认模块边界
- 采购业务核（缺口计算、状态机等确定性规则）：`src/procurement_core/AGENTS.md`
- Supervisor（上下文枢纽与条件路由）：`src/supervisor/AGENTS.md`
- Workers（七项责任，形态见 `DECISIONS.md` D02）：`src/workers/{intake,internal,sourcing,manufacturer,adjudicator,report,action}/AGENTS.md`
- Event Trigger（事件入口与去重窗口）：`src/monitor/AGENTS.md`
- Alert Engine（告警产生、去重与关闭）：目标目录见 `ARCHITECTURE.md`，创建前先确认模块边界
- Runtime 契约层：`src/contracts/AGENTS.md`
- Tools（Skill/MCP 封装）：`src/tools/AGENTS.md`
- Persistence / Infrastructure：`src/persistence/AGENTS.md`、`src/infrastructure/AGENTS.md`

边界不明确时先在 `ARCHITECTURE.md` 更新模块划分，再动代码或局部 `AGENTS.md`，不擅自创建未在 `ARCHITECTURE.md` 出现的新模块。
