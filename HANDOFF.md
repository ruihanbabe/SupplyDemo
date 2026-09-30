# HANDOFF.md — SupplyAgent 交接

> 更新：2026-09-30 ｜ 取代 2026-09-29 第二版
> 用途：新会话不看旧对话也能接上。先读本文件和 `AGENTS.md`，再按"下一步"定点读 `spec.md` 与对应 `contracts/Fxx.md`，不要通读全文。

## 1. 现在在哪
- 权威文档：`spec.md`（v0.3，§0–§6、§11 已定稿；§7–§9 深度点实验策略待定 P8）。Feature 清单与状态在 `features.json`（29 个，M1–M5）；每个 Feature 的 Contract 在 `contracts/`。
- 分支 `spec-v0.2-harness-reset`，最新提交见 `git log`；**本轮提交尚未推送**（推送前问用户）。
- 门禁现状：`make check` 131 过；`make services-up && make migrate && make test-integration` 11 过（迁移到 0006）。本机 Docker 用 colima，先 `colima start`。
- **已完成并验收**：F01 工程底座、F02 第二块板。
- **待验收**（2026-09-29～30 按用户授权自动推进；全部 mock，无任何真实 LLM / 分销商调用）：

| Feature | 代码 | 迁移 | 要点 |
|---|---|---|---|
| F03 环境内核 | `src/simenv/` | 0002 | `make sim-run SEED=42`；快照改在"日开盘"存 |
| F06 事件日志与 Artifact | `src/session/` | 0003 | 哈希链、失效传播、状态投影 |
| F08 模型适配层 | `src/models/` | — | `config/models.yaml` 的 quirks 为待真实联调核实的假设 |
| F07 工具代理层 | `src/tools/` | 0004 | 分销商只有固定样例后端；参数 Server 推迟 M2 |
| F17 计量 | `src/observability/` | 0005 | |
| F09 Runtime 薄版 | `src/runtime/` | — | 批内与案子间顺序执行（见 §5 待确认） |
| F16 上下文组装 | `src/context/` | — | token 为字符估算 |
| F21 确定性业务组件 | `src/business/` | — | 执行器用最小门控，F12 接入同一挂点 |
| F11 权限 L1/L2 | `src/permissions/` | 0006 | 能力令牌取代白名单；未写 mode 的案子默认 S3 只读 |
| F13 污点追踪 | `src/taint/` | — | 标签在 Artifact `meta.taint`；编排者手填的数量进不了采购单（G4） |
| F12 证据门控与审批令牌 | `src/gate/` | — | 人工操作入口 `HumanDesk`；令牌密钥取 `SUPPLYAGENT_APPROVAL_KEY` |
| F10 事件接入与案子管理（薄版） | `src/cases/` | — | 根因 = `caused_by` 链的根；F04 前同一天 Monitor 报的缺料合成一案 |
| F14 反馈与升级 | `src/feedback/` | — | 经 `CaseRouter(feedback=FeedbackLoop)` 每日运行；新增控制工具 `escalate_to_human` |
| F15 异常处理与恢复 | `src/resilience/` | — | 代理层与模型适配层传入 `Resilience` 才重试熔断；进程启动时调 `ProcurementExecutor.recover_pending_writes` |

- 配置集中处：重试与熔断在 `config/resilience.yaml`；反馈分级与升级阈值在 `config/feedback.yaml`；开案与路由在 `config/cases.yaml`；门控与审批在 `config/gate.yaml`；权限策略在 `config/permissions.yaml`，污点汇点与疑似指令模式在 `config/taint.yaml`；消融开关、Runtime 预算、上下文上限在 `config/harness.yaml`；工具配额在 `config/tools.yaml`；Policy 在 `config/policy.yaml`；品类比对清单在 `config/checklists/`。
- 测试共用装配在 `tests/kit.py`（整套 Runtime + ScriptedTransport）。

## 2. 下一步：继续自动推进 Harness（用户 2026-09-29 定）
1. 按依赖顺序继续：~~F11 → F13 → F12 → F10 → F14 → F15~~ → **F18 → F19 → F20 → F22 → F23**（开工前以 `features.json` 的 `depends_on` 为准）。下一个是 **F18 测试注入、回放与分叉**（spec §5.12）。
2. 每个 Feature：先写 Contract 并提交（`features.json` 状态改 `in_progress`）→ 实现 → `make check` + `make test-integration` 全绿 → 提交并改 `awaiting_acceptance`。不等确认，用户事后统一验收。
3. 用户已拍板：非 Harness 依赖一起做最小版（F21 已做；F19、F20 为 F23 依赖）；外部调用只用 mock 和固定样例；Contract 写了直接做。
4. 每做完两三个 Feature 更新一次本文件 §1 表格。

## 3. 代码规范（用户 2026-09-29 定，覆盖默认注释密度）
- 每一句代码都有注释（中文）。
- 每一段代码（函数 / 类 / 逻辑块）前写：功能说明、前后依赖说明（依赖谁、被谁用）、技术上完成了哪些部分。
- 每个代码文件顶部写整个文件的摘要汇总（Feature 完成时核对是否仍准确）。
- 只作用于新写 / 改写的代码，不回头改 F01/F02 已提交代码。

## 4. 协作节奏
- 不用 `/goal` 模式（Stop hook 空转耗 token）。
- 设计讨论时：每轮一个子系统、一段话 + 具体例子、≤2 个待决点，其余给默认值。给方案时列多个可行方案，说明技术实现、优缺点、对后续 Feature 的影响，给推荐。
- spec 已定策略不删不降级，砍减只指推迟；信息只写进单一权威文档，别处只留编号引用。

## 5. 待用户确认（验收时一并定）
- **F09 顺序执行**：批内任务与多个案子按固定顺序依次执行、不用线程。理由：线程交错会破坏可复现（§0.2）与全回放哈希链（§5.12）；Agent 思考不耗模拟时间（§4.2），结果不变只是更慢。"成批派发、整批结束才唤醒一次"与并发上限都保留。
- **F03 快照时点**：由原定"日收盘"改为"日开盘"（当日机制后、写入前），快照只写一次，分叉点就是"同一局面、谁都没动手"。
- **`mcp` 包**：用户已同意、自行安装中（官方 PyPI + `--only-binary cryptography`，本机 Intel Mac 无 cryptography 50 预编译包）；装好后 `make freeze`，再给 F07 Server 加标准传输包装。
- **F11 默认模式**：开案没写 `mode` 时按 S3 只读（最小权限），由 F22 路由写入 mode。
- **真实联调**（需授权，G5）：F08 quirks、F16 token 系数、F07 分销商真实后端都等首次真实调用后校准。

## 6. 已知问题
- 环境 `validate_write` 只接受 BOM 内料号：替代料能过 F12 草稿门控，但还不能真正提交到环境（F23 做替代采购时一并处理）。
- `features.json` 中 F20 的名称仍写"板级上下文 YAML"，与 F02 定的"原文存档、不预先结构化"冲突；做到 F20 写 Contract 时一并改。
- `compose.yaml`、`CODING_RULES.md`、`requirement.txt` 仍引用已删除的旧文档（`DECISIONS.md` D14、`docs/spec/data-model.md` 等），`Makefile` 注释里也有 D14；待另行处理。
- 沙箱：pytest 启动会读 `.env`（`make check`、`make test-integration` 须在沙箱外执行），写 `data/hardware/raw/` 与 `rm` 也会被拦。
- zsh 不会按空格拆分变量，脚本里用函数或 Python。

## 7. 新会话开场可以这样说
"读 HANDOFF.md、AGENTS.md 和记忆，从 §2 的下一个 Feature（F18）继续自动推进 Harness。"
