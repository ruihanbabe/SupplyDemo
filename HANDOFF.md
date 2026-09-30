# HANDOFF.md — SupplyAgent 交接

> 更新：2026-09-30（第三版，取代同日第二版）
> 用途：新会话不看旧对话也能接上。先读本文件和 `AGENTS.md`，再按 §3 的"下一个"定点读 `spec.md` 与对应 `contracts/Fxx.md`，不要通读全文。
> 本文件只记进度、计划与待决事项；设计内容以 spec 节号与 Contract 为准，这里不复制。

## 1. 一句话现状
Harness 核心（M1–M3 的深度点机制）已全部实现并通过门禁，**18 个 Feature 待用户验收、9 个未开工**。全部用 mock、固定样例与脚本模型验证，**没有做过任何真实 LLM、分销商或 datasheet 下载调用**。

- 分支 `spec-v0.2-harness-reset`；本轮 24 个提交（`92ba9e8..HEAD`）**尚未推送**，推送前问用户。
- 门禁：`make check` 166 过；`colima start && make services-up && make migrate && make test-integration` 14 过；数据库迁移到 `0008`。
- 权威文档：`spec.md` v0.3（§0–§6、§11 定稿；§7–§9 深度点实验策略待定 P8）；Feature 清单与状态在 `features.json`；每个 Feature 的做什么 / 不做什么 / 验收证据在 `contracts/Fxx.md`。

## 2. 进度总表（29 个）

### 2.1 已验收（2）
| Feature | 代码 |
|---|---|
| F01 工程底座 | `src/` 包结构、`Makefile`、`hw_part` 管线 |
| F02 第二块板（bms-c1）与板级 README 原文存档 | `data/hardware/raw/` |

### 2.2 待验收（18，按完成顺序；验收时逐个看 Contract 的"验收证据"并跑命令）
| Feature | 代码 | 迁移 | 验收时要看的要点 |
|---|---|---|---|
| F03 环境内核 | `src/simenv/` | 0002 | `make sim-run SEED=42`；快照时点改为"日开盘"（§4 待确认） |
| F06 事件日志与 Artifact | `src/session/` | 0003 | 哈希链、失效传播、状态投影 |
| F08 模型适配层 | `src/models/` | — | `config/models.yaml` 的 quirks 是待真实联调核实的假设 |
| F07 工具代理层 | `src/tools/` | 0004 | 分销商只有固定样例后端；`mcp` 标准传输未包装（§4） |
| F17 计量 | `src/observability/` | 0005 | |
| F09 Runtime | `src/runtime/` | — | 批内与多案顺序执行（§4 待确认） |
| F16 上下文组装 | `src/context/` | — | token 为字符估算；F20 起有 `[记忆]` 分区，F22 起卡片显示修订后目标与计划任务状态 |
| F21 确定性业务组件 | `src/business/` | — | 影响分析、Sourcing、Policy、等级汇总、采购执行器 |
| F11 权限 L1/L2 | `src/permissions/` | 0006 | 能力令牌取代白名单；未写 mode 的案子默认 S3（§4 待确认） |
| F13 污点追踪 | `src/taint/` | — | 编排者手填的数量进不了采购单（G4）；F20 起长期记忆接受 H1 / H3 确认 |
| F12 证据门控与审批令牌 | `src/gate/` | — | 人工入口 `HumanDesk`；令牌密钥取 `SUPPLYAGENT_APPROVAL_KEY` |
| F10 事件接入与案子管理（薄版） | `src/cases/` | — | F04 之前同一天 Monitor 报的缺料合成一案 |
| F14 反馈与升级 | `src/feedback/` | — | 反馈卡片、偏差比对、代码强制升级、`escalate_to_human` |
| F15 异常处理与恢复 | `src/resilience/` | — | 重试熔断、两步写恢复（`Runtime.start()` 调用） |
| F18 测试注入、回放与分叉 | `src/replay/` | — | 全回放哈希一致以同一运行号、无配额部分成功的运行为准 |
| F19 datasheet 知识库 A | `src/datasheets/` | 0007 | 只用合成样例 `tests/fixtures/datasheets/`；真实下载与 Docling 未接入 |
| F20 知识资产与知识库 B | `src/knowledge_base/`、`skills/`、`config/knowledge/` | 0008 | 术语表 5 品类、4 个 Skill、种子导入、提议 / 确认 / 待重验 / 撤销 |
| F22 编排者与 Runtime 装配 | `src/orchestrator/` | — | `build_runtime()` 一处装配；合并工具 env_lookup / po_action / request_human（S1 8 个、S3 6 个） |
| F23 子 Agent 与 LLM 函数 | `src/agents/` | — | 原料画像、替代检索（仅知识库 B 来源）、隔离的 Verifier、Resolver 三层、Explainer |

### 2.3 未开工（9）
F04 情景与异常、F05 模拟人工、F24 S3 互换问答、F25 Web 人工节点、F26 Web 时间线与配额面板、F27 Eval 最小框架、F28 对照配置与消融、F29 交付。计划见 §3。

### 2.4 现在能跑通什么（都在离线测试里）
- **S1 原料号换供应商**：Monitor 开案 → 编排者（脚本模型）写计划 → 派影响分析、比价、决策 → `po_action` 建草稿、送审（带预演）→ 主管经 `HumanDesk` 批准签发令牌 → 提交前重校验 → 提交（两步写）→ 到货后缺口消除才关闭。见 `tests/test_orchestrator.py`、`tests/test_gate.py`、`tests/test_cases.py`。
- **固定模板对照组**：`Runtime.template_driver()` 不用 LLM 跑完同一路径。
- **替代料验证**：原料画像 → 替代检索 → Verifier（结构隔离、作用域、引用校验、代码汇总等级）→ 门控按等级放行（B 级需 H1）。见 `tests/test_agents.py`。**替代料还不能真正提交到环境**（§5 已知问题 1）。
- **可靠性**：重试熔断、崩溃恢复不重复下单、测试注入、数据回放 / 全回放哈希比对、唤醒边界分叉。

## 3. 下一步计划（剩余 9 个）
工作方式不变（用户 2026-09-29 定）：每个 Feature 先写 Contract 并提交（`features.json` 改 `in_progress`）→ 实现 → `make check` 与 `make test-integration` 全绿 → 提交并改 `awaiting_acceptance`；Contract 写了直接做，只在设计分歧时提问；外部调用只用 mock 与固定样例；每做完两三个更新本文件。

| 顺序 | Feature | 依赖（均已完成，除注明） | 要做的主体 | 开工前要问 / 注意 |
|---|---|---|---|---|
| 1 | **F05 模拟人工**（下一个） | F03 | H1–H5 规则应答（按配置策略批准 / 驳回 / 确认 / 回复），全部走现有入口：`HumanDesk`（H1 替代确认、H2 审批）、`KnowledgeBase.confirm`（H1 记忆、H3 别名）、`GoalDesk`（H4）、计划员回复（H5，新增入口） | 无 |
| 2 | F04 情景与异常 | F03 | 情景 YAML、A1–A9 由环境机制产生（延误、买空、EOL、产能损失、插单、需求突增、datasheet 改版）、B1–B3 叠加注入、期望标注、两个基线与情景筛选；F10 的根因改由异常事件给出（`caused_by`）；F18 的注入规则并入情景 YAML | 期望标注格式按 spec §7 "开工时顺带定"写进 Contract |
| 3 | F24 S3 互换问答 | F19、F22、F23 | 只读节点组合、每条事实结论过引用校验、缺证据标"无法确认"、结论输出 | S3 是否放开 `request_human(memory)`（§4） |
| 4 | F26 Web 时间线与配额面板 | F06、F17 | 案子时间线（事件、卡片、Artifact）、当日配额用量 | Web 技术栈（`frontend/` 目录已存在，先看其中内容） |
| 5 | F25 Web 人工节点 | F06、F12、F20 | 配置化登录与角色、H1–H5 页面、用户发起请求（接 F22 路由）、审批页（含预演） | 同上 |
| 6 | F27 Eval 最小框架 | F04、F17、F18 | 三层代码评分、实验运行器（种子 × 配置 × 重复）、置信区间、回归门禁脚本 | **须先定 spec §7–§9 实验策略（P8）**；真实模型跑 Eval 需授权 |
| 7 | F28 对照配置与消融 | F22、F23、F27 | 单 Agent、细拆多 Agent、按 `config/harness.yaml` 各消融开关批量对照 | 依赖 F27 |
| 8 | F29 交付 | F18、F25、F26 | compose 一键启动全套、录好的演示回放、应用连接改用 F11 登录角色 | 无 |

## 4. 待用户确认（验收时一并定）
- **F09 顺序执行**：批内任务与多个案子按固定顺序依次执行、不用线程（保证可复现与全回放哈希链；"整批结束才唤醒一次"与并发上限都保留）。
- **F03 快照时点**：由"日收盘"改为"日开盘"（当日机制后、写入前），分叉点即"同一局面、谁都没动手"。
- **F11 默认模式**：开案没写 `mode` 时按 S3 只读（最小权限）；Monitor 开案与 F22 路由都会写明模式。
- **F22 S3 工具**：S3 只允许 R0，所以问答模式没有 `request_human`（不能在问答里提议别名等记忆）；是否为这一项放开待定。
- **`mcp` 包**：用户同意、自行安装中（官方 PyPI + `--only-binary cryptography`；本机 Intel Mac 没有 cryptography 50 预编译包）。装好后 `make freeze`，再给 F07 Server 加标准传输包装。
- **需要授权才能做的真实调用（G5）**：F07 分销商真实后端；F19 真实 datasheet 下载 + Docling（新依赖）；F23 DigiKey 参数检索来源与真实模型跑子 Agent；F08 quirks、F16 token 系数等待首次真实调用后校准。

## 5. 已知问题
1. 环境 `validate_write` 只接受 BOM 内料号：替代料能过门控但还不能提交到环境（建议在 F04 或 F24 前补：替代料按"原料位号的替代"写入环境）。
2. `compose.yaml`、`CODING_RULES.md`、`requirement.txt` 与 `Makefile` 注释仍引用已删除的旧文档（`DECISIONS.md` D14、`docs/spec/data-model.md` 等）；F29 时一并清理。
3. 部分组件是可选接入的：单独构造 `SegmentRunner` / `CaseRouter` / `EvidenceGate` 时不传 planner、feedback、knowledge、claims 等就不生效；**正式运行一律用 `orchestrator.assembly.build_runtime()`**，它全部接好。
4. 沙箱：pytest 启动会读 `.env`，`make check` / `make test-integration` 须在沙箱外执行；写 `data/hardware/raw/` 与 `rm` 会被拦；`colima start` 也须在沙箱外。
5. zsh 不按空格拆分变量，脚本里用函数或 Python；BSD `sed` 不支持 `\b`，批量替换用 Python。
6. YAML 里 `0402` 这类值必须加引号（否则被读成数字，F20 术语表加载时会报错提示）。

## 6. 规范与协作（用户定，覆盖默认）
- **代码注释**：每一句代码都有中文注释；每段（函数 / 类 / 逻辑块）前写功能、依赖（依赖谁、被谁用）、完成了什么；每个文件顶部写摘要，Feature 完成时核对。只作用于新写 / 改写的代码。
- **协作**：不用 `/goal` 模式；设计讨论每轮一个子系统、一段话 + 例子、≤ 2 个待决点，其余给默认值；给多个方案并推荐；spec 已定策略不删不降级（砍减只指推迟）；信息只写进单一权威文档，别处留编号引用；**回复用中文**。

## 7. 配置与测试入口
- 配置：消融开关、预算、上下文上限 `config/harness.yaml`；编排者节点集合 / 工具集 / 固定模板 `config/orchestrator.yaml`；子 Agent 阈值 `config/agents.yaml`；术语表与品类 `config/knowledge/`，Skills `skills/`；datasheet 检索 `config/datasheets.yaml`；重试熔断 `config/resilience.yaml`；反馈与升级 `config/feedback.yaml`；开案与路由 `config/cases.yaml`；门控与审批 `config/gate.yaml`；权限 `config/permissions.yaml`；污点 `config/taint.yaml`；工具配额 `config/tools.yaml`；Policy `config/policy.yaml`；比对清单 `config/checklists/`。
- 测试装配：`tests/kit.py`（`build` 为 F09 起的最小 Runtime，`code_pipeline` 为代码节点链）；完整 Runtime 用 `tests/test_orchestrator.py` 的 `runtime()`（`build_runtime` + 配额不限 + 样例 datasheet + 脚本模型）。

## 8. 新会话开场可以这样说
"读 HANDOFF.md、AGENTS.md 和记忆，从 §3 的下一个 Feature（F05）继续推进，用中文回复。"
