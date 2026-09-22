# SupplyAgent 开发指南

> **文档契约** · 类型：规则层 · 读取：首次会话 + 需要执行验证、拆分 Feature 或准备环境时按节读
> 更新：工具链、验证层级或 Feature 编排契约变更时由 Claude 写入
> 独占：命令入口边界、环境约定、四层验证在 Feature 上的落地方式、修复级与升级上限、Feature 六字段契约、并行边界、上下文投影
> 不收录：命令实现（见 `Makefile`）、验证四层的定义（见 `DECISIONS.md` D14）、架构事实、决策理由、当前状态、文档写作规则（见 `CODING_RULES.md`）

> 位置说明：本文件位于仓库根目录（`AGENTS.md` / `README.md` / `PROGRESS.md` 均按此路径引用）。

## 标准命令

`Makefile` 已建立，是命令的**唯一权威入口**，以 `make help` 列出的目标为准；本文档只说明入口边界与预期结果，不复制其实现。

```bash
make help
```

边界说明：

- `make setup` 建 `.venv`（缺失时）、按锁文件或 `requirement.txt` 装依赖、在缺少 `.env` 时由 `.env.example` 复制一份。不碰服务，不装系统级软件。
- `make status` 只报告解释器、虚拟环境、`.env` 与源码文件数；不启动任何东西。服务状态用 `make services-status`。
- `make check` 是 `compile` + `lint` + `test` 的离线门禁，对应 `DECISIONS.md` D14 的 ①② 层。
- `make run` 启动本地 FastAPI；`make health` 只有在服务已启动且就绪时通过。
- `make freeze` 把当前虚拟环境固化为 `requirements.lock.txt`。

源码或测试目录为空时，`compile` / `lint` / `test` 会打印「跳过」并以 0 退出。**跳过不等于通过**，记录证据时必须写明跳过原因，不得当作已验证。

## 环境

**开发在本地进行**，借用的 GPU 服务器不再是默认环境，仅在后续确有需要时启用（见 `DECISIONS.md` D16）。

- 语言/运行时：Python 3.11（已锁定，见 `DECISIONS.md`）。虚拟环境是项目根的 `.venv`，由 `make setup` 创建；解释器路径通过 `PYTHON` 变量覆盖。手工进入环境用 `source .venv/bin/activate`，但**日常操作走 `make`，不依赖是否已激活**——Makefile 内部一律用 `.venv/bin/python`。
- 将 `.env.example` 复制为 `.env`（`make setup` 会自动做），密钥不得进入 Git。进程环境变量优先于 `.env`。
- LLM Provider 通过抽象 `ModelProvider` 接口接入，不在代码或文档中写死具体厂商；启用开关与凭据走 `SUPPLYAGENT_LLM_*` 系列环境变量，变量名以 `.env.example` 为准。模型通过统一 ModelBackend 接入，能力与路由使用版本化运行配置（见 `DECISIONS.md` D05）。**本地不跑任何模型推理**，全部外部调用。
- 服务镜像和拓扑以 `compose.yaml` 为准：PostgreSQL 16.6 与 Redis 7.4.2，端口只绑 `127.0.0.1`。容器运行时是 colima（macOS Virtualization.framework）+ Docker CE 29.7.1。
- 依赖来源：`requirements.lock.txt`（由 `make freeze` 生成）；当前已装版本与变更记录见根目录 `requirement.txt` 台账。机器上的既有环境不等于项目契约。
- 组件版本必须与将来启用的任何远端环境保持一致，否则「本地通过」不能作为远端行为的证据（见 D16 约束）。
- MVP 不引入通用向量库 / RAG（见 `DECISIONS.md` D11，Infrastructure 仅 LLM / PostgreSQL / Redis / 供应商 API）。

## 服务

```bash
make services-up
```

`services-up` / `services-status` / `services-smoke` / `services-down` 四个目标封装 `docker compose`，拓扑权威是 `compose.yaml`。`services-smoke` 会连接真实 Redis / PostgreSQL，只证明连通性，不证明 schema 或业务正确性。

跑 `DECISIONS.md` D14 的 ③ 层（集成故障注入）前，服务必须先 up 且健康检查通过。`services-down` 默认保留数据卷（`supplyagent-pgdata` / `supplyagent-redisdata`），需要干净重来时显式删卷。

## 可选模型验证

`make model-smoke` 会调用真实模型。执行前必须获得用户明确授权，并确认凭据和潜在费用。

## 验证边界

`make check` 覆盖 `DECISIONS.md` D14 的 ①② 两层：离线语法编译、ruff、离线测试。它**不证明**真实 LLM 调用、Redis / PostgreSQL 的应用级接入、外部供应商 API、或业务正确性（缺口计算、报价、审批链路的实际正确性）——那些属于 ③④ 层。

`ruff` 与 `pytest` 已装入 `.venv`，但在 `src/` 与 `tests/` 尚无内容时会打印跳过。装配完成后 lint 即为必经子阶段，不得再跳过。

## Feature / Task 分层

Feature 是 Harness 和项目进度层的最小可独立验收单元，不是 Agent 内部 task decomposition 的最小步骤。Harness 管理 **WHAT + DONE**：Feature 描述目标行为，verification 定义完成标准；Codex 管理 **HOW**，并根据当前仓库状态自行拆解 implementation tasks。

功能清单使用 [`docs/features.json`](docs/features.json)（待建）。每个项目必须具备 `id`、`behavior`、`depends_on`、`verification`、`state` 和 `evidence` 六个字段：

```json
{
  "id": "F03",
  "behavior": "<可观测的目标行为>",
  "depends_on": ["F01"],
  "verification": {
    "contract": "<静态契约检查命令>",
    "offline": "<离线测试命令>",
    "integration": "<集成故障注入命令>",
    "e2e": "<授权端到端命令，或 null>"
  },
  "state": "planned",
  "evidence": {
    "contract": null,
    "offline": null,
    "integration": null,
    "e2e": null
  }
}
```

`depends_on` 记录直接依赖的 Feature `id` 列表；无依赖必须显式写 `[]`，不得省略该字段。新增 Feature 时必须同时声明 `depends_on`，不得留空绕过。

`docs/features.json` 建立前，用 `PROGRESS.md` 的任务看板过渡；过渡期不适用下述自动编排表述（编排 Harness 尚未实现）。

### 完成定义与终止校验

代码完成不等于 Feature 完成。Feature 只有在其**所需的全部验证层级**均成功、并由 Harness 根据执行证据判定时，才能变为 `passing`；Agent 的自我评价、编译成功或部分测试不是完成证据。验证必须按层序执行：上一层失败、未执行或被阻塞时，不得进入下一层，也不得宣称完成。核心行为未通过这些校验前，不得开展顺便重构、风格整理或性能优化。

层级定义见 `DECISIONS.md` D14，四层是全仓唯一的验证层级模型：

| 层 | 键名 | 内容 | 典型命令 |
|---|---|---|---|
| ① 静态契约 | `contract` | 输入输出 schema、引用完整性、状态合法迁移、模块依赖方向检查 | `make compile`、`make lint`、契约检查脚本 |
| ② 离线测试 | `offline` | 固定数据与 mock，覆盖身份、算术、权限、预算。**不访问供应商** | `make test` |
| ③ 集成故障注入 | `integration` | 本地真实 PostgreSQL / Redis / 目标业务服务，注入超时、进程终止、并发与响应丢失 | `make services-up` + 故障注入测试 |
| ④ 授权端到端 | `e2e` | 真实供应只读接口 + 自己控制的业务系统草稿写入，记录版本、地区、时间与结果 | 需用户明确授权后执行 |

**不是每个 Feature 都需要四层。** Feature 在 `verification` 中声明它需要哪几层；未声明的层记为 `not_applicable` 并说明理由，不得留空蒙混。但凡涉及写操作、幂等或恢复语义的 Feature，③ 层为必需；宣称"接通了某个外部系统"的 Feature，④ 层为必需。

③④ 需真实服务或外部授权。**被阻塞时如实记为 `blocked`，不得跳过后宣称完成**——`make check` 只覆盖 ①②，它通过不等于 Feature 通过。

`docs/features.json` 的 `verification` / `evidence` 按上表四个键名组织：

```json
{
  "verification": {
    "contract":    "<命令>",
    "offline":     "<命令>",
    "integration": "<命令>",
    "e2e":         null
  },
  "evidence": {
    "contract":    null,
    "offline":     null,
    "integration": null,
    "e2e":         "not_applicable: 本 Feature 不触达外部系统"
  }
}
```

每层的 `verification` 必须是 Harness 可执行的命令或脚本入口，`evidence` 必须记录实际结果、运行时信号和证据位置/提交。应观察的信号包括：进程已启动并就绪，关键路径执行成功，数据库写入、文件操作等副作用正确，以及临时资源已清理（当 Feature 涉及该信号时）。

**硬门禁贯穿四层**：越权写入、重复草稿、伪造身份、无证据关键结论必须为零，不因层级低而豁免（见 `docs/product/acceptance-cases.md` §1）。

状态只能是 `planned`、`active`、`verifying`、`passing` 或 `blocked`。Agent 只能提交该项的 verification 请求，不得自行修改 `state`、`evidence` 或将其标记为 `passing`。Harness 按 `contract` → `offline` → `integration` → `e2e` 顺序执行：仅当所需层级全部通过时写入可追溯 `evidence` 并转为 `passing`；任一层失败时保留该层失败证据、停止后续层级并转为 `blocked`。失败信息必须包含失败命令/信号、观察到的结果、可疑原因和下一步修复建议；不可只记录"测试失败"。

### Feature 选择顺序与并行边界

- Harness 按 `depends_on` 做确定性拓扑排序选择下一个 `planned` 项：只有当某 Feature 的 `depends_on` 列表中所有 Feature 均为 `passing` 时，它才可被置为 `active`。拓扑排序是确定性运算，不引入具有自主判断权的“主 Agent”来决定顺序；某 Feature 卡住需要重新规划时，升级给用户决定，不由 Codex/Claude Code 自行决定转派方向。
- 默认每次仓库中仅一项处于 `active` 或 `verifying`。**仅当**同时满足以下条件时，允许多个 Feature 并行处于 `active`：
  1. 参与并行的各 Feature 之间没有 `depends_on` 关系（互不依赖，含间接依赖）；
  2. 每个并行 Feature 在独立 git 分支上开发，不共享同一工作树；
  3. 各分支实现前已根据 `ARCHITECTURE.md` / 模块 `AGENTS.md` 确认不会修改同一份共享文件（如 `src/contracts/` 的跨层共享类型、`src/supervisor/` 等跨 Feature 公共模块）；出现潜在重叠时改为串行或先协调接口。
- 每个并行分支必须独立跑完该 Feature 所需的全部验证层级并转为 `passing` 后才能合并；合并顺序按 `depends_on` 与实际完成时间确定，合并前需在目标分支重新跑一次该 Feature 的验证，确认合并未破坏其证据。
- 跨 Feature 修复需要隔离：如果某 Feature 失败是因为其依赖的上游 Feature 本身有缺陷，不得在当前 Feature 的任务里顺手修改上游代码；应单独开任务、单独走完该 Feature 所需的全部验证层级，避免证据链混淆。

### 每 Feature 的上下文投影

实现某个 Feature 时，避免 context 过载导致判断被无关决策干扰。**禁止在实现单个 Feature 时完整读取 `DECISIONS.md`、`ARCHITECTURE.md`、`requirements.md` 三份文件的全文**，按以下定点读取流程执行：

1. 打开 `docs/features.json`，只读该 Feature 自己的条目，取出它的 `context_refs` 字段（`decisions` / `architecture_sections` / 可选 `requirements_sections`）。
2. 对 `context_refs.decisions` 里的每个 ID（如 `D07`），在 `DECISIONS.md` 里 `grep '^## D07'` 定位到该决策的标题行，只读取从这一行到下一个 `## ` 标题之前的内容。编号稳定，不依赖标题其余文字。
3. 对 `context_refs.architecture_sections` 里的每个章节标题（如「一级逻辑模块」），在 `ARCHITECTURE.md` 里定位对应的 `## ` 标题，只读取该章节到下一个同级或更高级标题之前的内容。`requirements_sections`（如 `2`）对 `docs/product/requirements.md`（编号章节 `## N.`）按同样方式定点读取。
4. 如果该 Feature 有 `depends_on`，额外读取被依赖 Feature 在 `docs/features.json` 里的条目（了解上游产出的接口），但不需要读被依赖 Feature 关联的 `context_refs` 内容。

例外：仅当某次调试明确怀疑是“决策理解错了”、且已按上述方式定点读取仍无法确认时，才允许临时读整份文件排查；排查完成后仍按第 1–4 条方式继续后续工作，不得把整份文件保留在长期上下文里。

此机制与「角色只拿到按策略投影出的最小上下文」同源。`docs/features.json` 顶层 `_context_refs_note` 记录 `context_refs` 字段的数据契约（待建）。

### 自动修复与升级上限

修复重试的计数单位是**修复级**，与上节的验证层是两套东西，不要混淆：验证层决定 Feature 能否 `passing`，修复级只约束"同一个问题允许自动试几次"。

- 修复级固定为四级递进：**A** 语法与类型检查（`make compile`）→ **B** lint（已装配时为必经阶段，`make lint`）→ **C** 单元测试（不连网络，`make test`）→ **D** 模块/集成层测试（视 Feature 是否需要连真实服务）。其中 A、B 落在验证层 ① 内，C 落在 ②，D 落在 ③。出错原则上从失败的那一级继续修复，不强制从头重跑；**但修复动作若改变了函数签名或接口，必须强制回退重跑更早的级别**，不得由模型自行判断是否需要回退。
- 四级分别计数、互不共享配额：每个 Feature 在每级的自动修复尝试上限各为 3 次。某一级达到上限后不得继续在该级自行尝试，也不得跳到下一级掩盖失败，必须停在该级并产出结构化升级报告，然后停下等待用户处理；不得为不同流程各自发明上报格式（结构化升级报告的具体字段格式待设计，见 `AGENTS.md`「全局硬约束」）。
- B 级尚未装配时记为跳过并在 `evidence.contract` 中注明「lint 未装配」，不视为阻塞；一旦装配则必须纳入门禁，不得再跳过。
- 该结构化升级报告统一覆盖两类触发场景：(i) 本节所述任一验证级别自动修复超过重试上限；(ii) 运行时角色/模块判断自身能力或权限不匹配（见 `src/contracts/AGENTS.md`）。两者都不得自行决定下一步转派给谁，一律停下等待升级处理。
- 失败与修复记录写入 `PROGRESS.md` 验证记录表格的“失败原因”与“修复动作”列，保留审计痕迹；记录时须注明具体是哪个修复级（A/B/C/D）以及所属验证层。

功能项以一次会话可完成为粒度校准：“运营人员可以按 SKU 查询当前库存”是合适的 Feature；“实现 Harness”过粗，“给 `ToolResult` 加一个字段”过细。创建文件、实现函数、增加字段等 implementation steps 不应机械提升为 Feature；只有当该步骤本身具有独立业务价值和独立验收标准时，才可作为 Feature。

以上是开发 orchestration 契约；当前编排 Harness 与 `docs/features.json` 均未建立，因此不得将此规则表述为已自动执行。待实现该能力时，具体数据格式、scheduler 和 validator 行为应进一步下沉到对应代码模块附近的 `README.md`。
