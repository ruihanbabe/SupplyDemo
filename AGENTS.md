# SupplyAgent Codex 入口

> **文档契约** · 类型：入口层 · 读取：每次会话启动必读，全文
> 更新：全局硬约束或模块划分变更且用户明确授权时
> 独占：Codex 全局行为硬约束、新会话启动流程、顶层文档与模块入口索引
> 不收录：架构事实、决策理由、需求条款、当前状态、命令实现（各自见索引指向的文档）

**元件采购决策助手**：采购需求 → 多 Agent 并行核验 → 采购建议 → 风险进入预警流 → 人工审批后执行。

这是**技术展示项目**，产品形态服务于技术清单。任何设计改动先对照 `docs/product/requirements.md` §2 的技术点表自查：会不会让某一行退化为「演示不出来」。准确状态见 `PROGRESS.md`。

## 新会话启动

1. 阅读本文件、`README.md`、`PROGRESS.md`；`DECISIONS.md` 只浏览决策标题（`grep '^## ' DECISIONS.md`）建立索引，具体条目按需定点读取，不在启动阶段通读全文。
2. 运行 `git status --short --branch`，保护已有未提交修改。
3. 运行 `make status` 确认解释器、`.venv` 与 `.env` 就位；需要真实服务时再 `make services-up` + `make services-smoke`。开发环境在本地，不连远程服务器（见 `DECISIONS.md` D16）。
4. 根据任务确定所属模块（参照 `ARCHITECTURE.md`「模块」表），若该目录有局部 `AGENTS.md` 先读它；内容较薄时以 `ARCHITECTURE.md` 对应模块行为准。
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
- 读取 `DECISIONS.md`、`ARCHITECTURE.md`、`requirements.md` 时按标题/章节号定点读取，不通读全文：`DECISIONS.md` 各条目以 `## Dxx` 开头，可用 `grep` 定位。仅当怀疑"决策理解有误"且定点读取仍无法确认时，才临时读整份文件排查，排查完不得把全文留在长期上下文。
- 每份文档开头的**文档契约块**（`> **文档契约**` 四行）规定了该文档的读取时机、更新时机、独占信息与不收录信息；写入任何信息前先按该块判断归属，规则见 `CODING_RULES.md`。同一信息只在其 SSOT 文档保留正文，其他文档只写编号引用或一句话指针。
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

**产品与架构**

- 技术展示清单（**锚点**）、产品形态、FR / BR、纵向切片范围、数据现状：`docs/product/requirements.md`
- 模块、契约类型、依赖方向、并行与失败语义、权限分层、不变量：`ARCHITECTURE.md`
- 决策与理由：`DECISIONS.md`（`## Dxx` 定位）
- 跨文档未决项：`docs/OPEN-QUESTIONS.md`（`Q-xx`）
- 领域术语：`docs/product/GLOSSARY.md`
- 验收用例：`docs/product/acceptance-cases.md`

**接口与数据契约**（实现前必读对应章节）

- 模型端口、工具 schema、结果信封、错误模型：`docs/spec/interfaces.md`
- Run 状态取值与合法迁移：`docs/spec/state-machine.md`
- PostgreSQL 表结构与约束、审计表只插入的强制方式：`docs/spec/data-model.md`
- HTTP 响应包络与端点清单：`docs/spec/http-api.md`
- 界面行为契约：`docs/spec/ui-contract.md`（Web 未开工，见 Q-07）

**状态与治理**

- 当前状态、阻塞与下一步：`PROGRESS.md`
- Feature 队列与验收证据：`docs/features.json`
- 开发命令、验证层级与代码规范：`DEVELOPMENT.md`
- 文档记录规则与 SSOT 矩阵：`CODING_RULES.md`

**代码模块**：落点见 `ARCHITECTURE.md`「模块」表。已有代码的目录带局部 `AGENTS.md`；未建目录不预先创建。
