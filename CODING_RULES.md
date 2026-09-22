# Codex 开发文档记录规则

> **文档契约** · 类型：规则层 · 读取：首次会话 + 需要写任何文档/注释前，按节读
> 更新：文档治理规则变更且用户明确授权时
> 独占：文档职责矩阵、SSOT 归属、题头契约块规范、文字记录边界
> 不收录：架构事实、设计决策、需求条款、当前状态、命令

> **适用范围**：本文件仅约束 Codex 在本项目中编写代码注释、开发文档、报告等非代码文字时的记录方式与内容边界。它不规定 Codex 如何执行任务、设计架构、验证完成或修改状态；这些要求由 `AGENTS.md` 及 `DEVELOPMENT.md` 维护。

## 文档体系：类型、读取时机与更新时机

每份 Markdown 文档的开头必须有一个四行的 **文档契约块**（可见 blockquote），格式固定为：

```markdown
> **文档契约** · 类型：<入口|状态|队列|契约|规则|模块|参考|归档> · 读取：<何时、用什么姿势读>
> 更新：<明确的触发条件> 由 <用户授权的维护者|Harness> 写入
> 独占：<本文是唯一正文来源的信息要素>
> 不收录：<明确不该出现在本文的信息要素>
```

「独占」与「不收录」两行是防冗余的执行依据：写入任何信息前，先确认目标文档的「独占」覆盖该信息要素；若命中某文档的「不收录」，说明写错了地方。

### 全局文档职责表

| 层 | 文档 | 读取时机 | 更新时机 | 写入方 |
|---|---|---|---|---|
| 入口 | `AGENTS.md` | 每次会话启动，全文 | 全局硬约束变更 | 用户授权的维护者 |
| 状态 | `PROGRESS.md` | 每次会话启动，全文 | **Feature 完成、遇阻塞、验证失败后即时** | Codex |
| 队列 | `docs/features.json` | 取任务时只读自己那条 + 被依赖条目 | Feature 状态流转 | Harness |
| 契约 | `ARCHITECTURE.md`、`DECISIONS.md` | **Feature 开始时定点读，禁止通读** | 架构或决策变更 | 用户授权的维护者 |
| 契约 | `docs/OPEN-QUESTIONS.md`（跨文档未决项） | 遇到 `Q-xx` 引用时定点读 | 未决项新增、冻结或被产品决定取代 | 用户授权的维护者 |
| 契约 | `docs/product/requirements.md`、`docs/product/acceptance-cases.md`、`docs/product/GLOSSARY.md` | 按 FR/BR/EV 编号或术语定点读 | 需求条款、验收用例或术语变更 | 用户授权的维护者 |
| 契约 | `docs/spec/interfaces.md`、`docs/spec/state-machine.md`、`docs/spec/data-model.md`、`docs/spec/http-api.md`、`docs/spec/ui-contract.md` | 实现对应接口/状态/表/端点/视图时定点读 | 接口签名、状态迁移、表结构、端点或呈现规则变更 | 用户授权的维护者 |
| 规则 | `requirement.txt`（依赖与环境台账） | 准备环境或增删依赖时按节读 | **安装/升级/删除依赖后即时** | Codex |
| 参考 | `docs/research/procurement-platforms.md` | 选型或评估配额时定点读 | 重新核对平台能力或条款 | 用户授权的维护者 |
| 规则 | `DEVELOPMENT.md`、本文件 | 首次 + 按节 | 工具链或治理规则变更 | 用户授权的维护者 |
| 模块 | `src/*/AGENTS.md`、`src/*/ARCHITECTURE.md` | 进入该模块前 | 该模块接口或不变量变更 | Codex |

### SSOT 矩阵：每个信息要素只有一处正文

原则：**一个信息要素只有一处正文；其他文档只能出现编号引用（如「见 D07」「见 FR-04」）或一句话指针，不得复制正文。**

| 信息要素 | 唯一正文 | 允许的引用形式 |
|---|---|---|
| 模块职责 / 数据所有权 / 依赖方向 | `ARCHITECTURE.md` 一级逻辑模块表 | `src/*/ARCHITECTURE.md` 展开本模块那一行 |
| 模块不变量（invariant） | 见下方「不变量的双写分工」 | — |
| 设计决策与其理由 | `DECISIONS.md` | 只写「见 D0x」，不复制理由正文 |
| Codex 行为硬约束 | `AGENTS.md` | 其他文档不重述，可引用条目 |
| 技术展示清单 / 产品形态 / 非范围 / 切片范围 / 数据现状 | `docs/product/requirements.md` | 引 T / FR / BR 编号或章节号 |
| 验收用例 EV / 判定原则 / 追踪矩阵 | `docs/product/acceptance-cases.md` | 只写「见 EV-0x」 |
| 工具 schema / ToolResult 信封 / 错误码 | `docs/spec/interfaces.md` | 引工具名 |
| 任务状态取值与合法迁移 | `docs/spec/state-machine.md` | 引状态名 |
| PostgreSQL 表结构与约束 | `docs/spec/data-model.md` | 引表名或约束名 |
| HTTP 响应包络、端点清单、错误映射 | `docs/spec/http-api.md` | 引路径或 `error.code` |
| 视图清单、交互映射、不确定性标注规则 | `docs/spec/ui-contract.md` | 引视图名或 warning 取值 |
| 依赖清单与版本台账 | `requirement.txt`（机器真相为 `requirements.lock.txt`） | 一句指针 |
| 服务拓扑 | `compose.yaml` | 一句指针 |
| 采购平台能力与配额、ERP 候选 | `docs/research/procurement-platforms.md` | 引平台名 |
| 当前状态、阻塞、验证记录 | `PROGRESS.md` | 其他文档一律不写状态 |
| Feature 清单、依赖、验收证据 | `docs/features.json` | `PROGRESS.md` 不复制 Feature 列表 |
| 命令入口与实现 | `Makefile` | `DEVELOPMENT.md` 只说明入口边界与预期结果 |
| 环境变量与服务拓扑 | `.env.example`、`compose.yaml` | `DEVELOPMENT.md` 只说明用途 |
| 数据现状与已知局限 | `data/supplychain/normalized/projects.yaml` | 各处一句指针 |
| 原始数据文件完整性 | `data/supplychain/domdata/MANIFEST.json`（机器生成） | 不手工抄录哈希 |
| 领域术语定义 | `docs/product/GLOSSARY.md` | 引术语名 |
| 跨文档未决项、当前对策、冻结条件 | `docs/OPEN-QUESTIONS.md` | 只写「见 Q-0x」，不复制正文；单文档内部的局部待定项留在该文档「待定项」节 |

### 不变量的双写分工

`ARCHITECTURE.md` 模块表的「必须维护的 invariants」列与 `src/*/AGENTS.md` 的「不变量与 contract」是有意保留的双份，但分工必须严格：

- **顶层表**：每条一句话摘要，作用是跨模块 review 时能一屏看全。
- **模块 `AGENTS.md`**：只写顶层表没有的实现级细节、边界条件和验证方式；**不得复述顶层表已有的句子**。
- 两者冲突时以顶层表为准，并同步修正模块文件；发现漂移即视为待修复缺陷。

### 不为「实现过程」新建文档

实现过程中的排查经过、试错记录、临时结论**不进入任何 Markdown 文档**。四个既有出口已经完整覆盖：

- 过程与变更经过 → Git commit message 与分支/PR 描述
- 沉淀下来的设计结论 → `DECISIONS.md` 新增条目
- 当前状态、阻塞、失败原因与修复动作 → `PROGRESS.md` 验证记录表
- 验收证据与位置 → `docs/features.json` 的 `evidence` 字段

不得新建 `NOTES.md`、`WORKLOG.md`、`TROUBLESHOOTING.md` 一类的过程日志文件。

当前阶段仅持续追加用户逐条提出的规则。Codex 不主动重构、归类、删除、压缩或规范化本文件中的内容；最终由用户手动删除多余部分并完成梳理。

## 编号治理

`FR-xx`、`BR-xx`、`EV-xx`、`Dxx`、`Fxx` 与 `Q-xx` 一经分配即**永久占位**：

- 条目删除后编号**不回收、不重排、不复用**，空缺就是空缺；
- 2026-09-22 产品形态重定：`FR` / `BR` / `EV` 全套重写，旧编号不再有效；`Q-02` 已废止（见 `docs/OPEN-QUESTIONS.md`「已废止编号」）；`D01` / `D02` / `D05` / `D10` 含义已改写，旧引用按当前正文解读；
- 新增条目一律取当前最大编号 +1，不填补空缺；
- 需要说明某个编号为何消失时，写在该编号所属文档的「已删除编号」处，不改动其他编号。

理由：编号是跨文档引用的唯一锚点。一次重排会让 `docs/features.json` 的 `context_refs`、模块 `AGENTS.md` 的前置阅读清单和测试注释同时失效，且失效是静默的。

`Dxx` 只改写含义、不重排位置；含义改写的编号在 `DECISIONS.md` 题头列出。

## 跨会话与多 Agent 初始化契约

以下是初始化阶段应建立和维护的文档规则，不表示仓库已经采用某个具体依赖、虚拟环境、测试或质量工具。

### 机器可执行事实优先

- 运行、开发、测试、检查和环境准备应首先由机器可执行命令定义；Markdown 只说明入口、边界和预期结果，不维护第二套命令事实。
- 初始化时应明确 runtime/dependency contract：Python 版本范围、依赖来源、环境创建方式、配置入口和外部服务边界。
- 推荐收敛为 `make setup`、`make dev`、`make test`、`make check` 等统一入口；实际 target 名称必须以仓库的 `Makefile` 为准。
- 若仓库尚无可复现依赖定义、锁文件或环境创建入口，必须明确记录为初始化缺口，不得把机器上的既有环境当作项目契约。

### 状态、任务与测试契约

- `PROGRESS.md` 只记录当前状态：最新 Git checkpoint、实际验证结果、进行中事项、已知问题、阻塞和下一步；完整历史由 Git 保存。
- 任务分解、优先级与验收标准由 `docs/features.json`（Feature 队列）与 `PROGRESS.md` 任务看板承担，**不另建 `TASKS.md`**：三套任务事实源会立刻漂移。`docs/features.json` 建立前用任务看板过渡。
- 测试是开发 contract。每个初始化阶段至少要说明测试命令、当前结果、未覆盖边界和阻塞原因；不得把 mock、语法编译或历史成功结果写成完整验收。
- 完成状态必须以实际执行的分层验证为准，不采信 agent 自评或“代码已写完”。`PROGRESS.md` 应记录每层的命令、实际结果、证据边界与阻塞情况。
- 验证失败记录必须提供可操作信息：失败的命令或信号、观察结果、可疑原因和下一步修复方向。

### 初始化验收与检查点

- 初始化完成后应创建 Git checkpoint。提交信息必须说明完成了什么初始化工作，以及为何这样组织；不提交凭据、原始 PII 或本地运行数据。
- 初始化 acceptance checklist 至少覆盖：解释器可识别、依赖来源可定位、配置模板存在、统一命令可列出、测试入口可执行、当前验证结果已记录、状态与任务入口可定位。
- 如果任何一项未完成，应在 `PROGRESS.md` 中写明阻塞、影响和下一步，而不是以“初始化完成”替代实际证据。

### Hot-start 原则

- 新窗口或新 Agent 应只需读取 `AGENTS.md`、`PROGRESS.md`、`DECISIONS.md`、任务入口及当前模块文档，就能知道从哪里开始、如何运行、如何验证和下一步做什么。
- 禁止重复维护初始化事实：命令以 `Makefile` 为准，服务拓扑以 `compose.yaml` 为准，依赖以 `requirements.lock.txt` 为准，当前状态以 `PROGRESS.md` 为准，任务验收以 `docs/features.json` 为准，稳定设计约束以 `DECISIONS.md` 为准，完整历史以 Git 为准。
