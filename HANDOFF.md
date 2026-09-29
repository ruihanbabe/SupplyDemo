# HANDOFF.md — SupplyAgent 交接

> 更新：2026-09-29（第二版）｜ 取代同日早先版本
> 用途：新会话不看旧对话也能接上。先读本文件和 `AGENTS.md`，再按"下一步"定点读 `spec.md`，不要通读全文。

## 1. 现在在哪
- 权威文档：`spec.md`（v0.3，§0–§6、§11 已定稿；§7–§9 深度点实验策略待定 P8）。Feature 清单与状态在 `features.json`（29 个，M1–M5）；每个 Feature 的 Contract 在 `contracts/`。
- 分支 `spec-v0.2-harness-reset`，已推送到 GitHub（`origin` = `ruihanbabe/SupplyDemo`），最新提交 `5af7f36`。
- **已完成并验收**：
  - F01 工程底座（`89fdb6e`）：`src/` 只有 `knowledge`、`persistence`、`infrastructure`；alembic 新基线 `0001` 只建 `hw_part` + 只读角色 `supplyagent_app`；`make check` / `test-integration` 全绿。
  - F02 第二块板（`a32d145`、`5af7f36`）：bms-c1 原理图入库（55 料号 / 154 贴装位；hackrf 62 / 297）；两块板 README 原文只读存档（`make fetch-docs`），板级上下文**不预先结构化**，按需经文档工具读取，抽取格式留到细化知识库时定（spec §1.3、§6 已回写）。
- **待验收**（2026-09-30 自动推进，Contract 在 `contracts/`，状态见 `features.json`；全部 mock，无真实外部调用）：
  - F03 环境内核 `src/simenv/`（迁移 0002，`make sim-run SEED=42`）；快照改在"日开盘"存。
  - F06 Session 事件日志与 Artifact `src/session/`（0003）。
  - F08 模型适配层 `src/models/`（端点差异 quirks 为待真实联调核实的假设）。
  - F07 工具代理层 `src/tools/`（0004）：分销商只有固定样例后端；参数 Server 推迟 M2；MCP 标准传输需装 `mcp` 包（新依赖，待同意）。
  - F17 计量 `src/observability/`（0005）。
  - F09 Runtime 薄版 `src/runtime/`：**批内与案子间顺序执行、不用线程**（为保证可复现与全回放哈希链，Contract 默认值待确认）。
  - 消融开关与 Runtime 预算集中在 `config/harness.yaml`。

## 2. 下一步：F03 起自动推进 Harness（用户 2026-09-29 定）
1. **F03 环境内核**（已完成，待验收）：架构采用方案 A——纯 Python 内存领域模型按天推进（注入 → 到货 → 开工领料/缺料停线 → 订单到期）；Postgres 只存 `env_snapshot`（每日整状态 JSONB + 哈希）与 `env_write`（外部写入只追加日志 + 幂等键）；崩溃恢复 = 载入最近快照 + 重放当日写入；分叉 / 预演 = 载入快照。分五小步：① 领域模型 + 日推进（纯内存、离线测试）② 按种子生成两块板的计划/订单/初始库存 ③ 查询与写入接口 ④ 快照、分叉、写入日志与崩溃恢复（一次迁移）⑤ 结果指标。
2. **F03 完成后自动完成所有 Harness Feature**，按 `features.json` 的依赖顺序。Harness = F06–F18 + F12、F22、F23；第一个是 F06。用户已拍板：
   - 非 Harness 依赖 **一起做最小版**：F21（F10/F12/F15/F22 依赖）、F19、F20（F23 依赖）。
   - 外部调用 **只用 mock** 和固定样例，不产生任何真实 LLM / 分销商调用；真实联调等用户授权。
   - Contract **写了直接做**：每个 Feature 先写 Contract 并提交，不等确认直接实现；做完交运行证据，用户事后统一验收。
3. 大致依赖顺序参考：F03 → F06 → F08 → F07 → F17 → F09 → F16 → F21 → F11 → F13 → F12 → F10 → F14 → F15 → F18 → F19 → F20 → F22 → F23（开工前以 `features.json` 的 `depends_on` 为准）。

## 3. 代码规范（用户 2026-09-29 定，覆盖默认注释密度）
- 每一句代码都有注释（中文）。
- 每一段代码（函数 / 类 / 逻辑块）前写：功能说明、前后依赖说明（依赖谁、被谁用）、技术上完成了哪些部分。
- 每个 Feature 完成后，在涉及的每个代码文件顶部写整个文件的摘要汇总。
- 只作用于新写 / 改写的代码，不回头改 F01/F02 已提交代码。

## 4. 协作节奏
- 不用 `/goal` 模式（Stop hook 空转耗 token）。
- 设计讨论时：每轮一个子系统、一段话 + 具体例子、≤2 个待决点，其余给默认值。给方案时列多个可行方案，说明技术实现、优缺点、对后续 Feature 的影响，给推荐。
- spec 已定策略不删不降级，砍减只指推迟；信息只写进单一权威文档，别处只留编号引用。

## 5. 已知问题
- `features.json` 中 F20 的名称仍写"板级上下文 YAML"，与 F02 定的"原文存档、不预先结构化"冲突；做到 F20 写 Contract 时一并改。
- `compose.yaml`、`CODING_RULES.md`、`requirement.txt` 仍引用已删除的旧文档（`DECISIONS.md` D14、`docs/spec/data-model.md` 等），`Makefile` 注释里也有 D14；待另行处理（用户曾提出清理后又搁置）。
- 沙箱：pytest 启动会读 `.env`、写 `data/hardware/raw/` 与 `rm` 都会被拦，需在沙箱外执行或由用户在终端执行。
- zsh 不会按空格拆分变量（`set -- $r`、`P="docker ..."; $P` 都会失败），脚本里用函数或 Python。

## 6. 新会话开场可以这样说
"读 HANDOFF.md、AGENTS.md 和记忆，按 §2 开始 F03：先写 Contract 并提交，然后按五小步实现，之后按依赖顺序自动推进 Harness。"
