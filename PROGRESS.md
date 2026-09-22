# SupplyAgent 当前进度

> **文档契约** · 类型：当前状态 SSOT · 读取：每次会话启动
> 更新：实现、验证、阻塞或下一步变化时覆盖更新
> 独占：当前完成度、当前证据、阻塞和下一步
> 不收录：历史流水账、需求正文和设计理由；历史由 Git 保存

更新时间：2026-09-21。

## 当前结论

项目已完成采购确定性基础和部分 API。产品主线已定为**供应风险主动预警**（D01、`productinfo.md` §1），告警引擎、Harness 与 Durable Runtime 均未实现。旧的固定 Query/Detail/Research/Summary/Action Worker 目录只有占位文件，不代表已具备多 Agent 能力。

既有的缺口计算与版本化方案（F01—F07）在新主线中承担两个角色：库存告急告警的判据，以及告警派生的比价建议基础。它们的职责未变，不需要重写。

## 已有实现

- 本地 Python 3.11、Makefile、PostgreSQL 16、Redis 7 与 Alembic 迁移。
- 只读原始 BOM 数据、确定性规范化流水线和 PostgreSQL 导入。
- 需求展开、候选处理、缺口计算、报价规则、MOQ/包装/阶梯价和版本化方案。
- FastAPI 基础包络及项目、BOM、需求、Run、缺口和方案端点的**代码与测试**；对应 Feature F08 仍为 `planned`，未经验收，不得称“已完成”。
- LLM provider 基础 Adapter 与 2 次经授权的 GLM 工具调用冒烟；这不等于统一 ModelBackend 或多后端已完成。当前已关闭真实调用。

`docs/features.json` 中 F01—F07 为 `passing`，F08—F09 为 `planned`。Feature 状态只能由用户按四层验证授权流转（D14），文档工作不修改 state 或 evidence。

## 当前验证证据

| 范围 | 最近记录的证据 | 限制 |
|---|---|---|
| F01—F07 | 2026-09-21：`make compile` 通过；`make lint` All checks passed；`make test` 108 passed / 22 deselected；`make test-integration` 22 passed | 覆盖确定性业务核与持久化，未覆盖 Harness 与恢复 |
| F08（FastAPI） | 2026-09-21：①②③ 三层随全量套件一并通过（离线 108、集成 22 含 API 用例） | **④ 未执行；Feature 仍为 `planned`，等待用户按 D14 授权流转** |
| 真实模型 | 2026-09-18：2 次 GLM 调用成功并触发工具调用 | 单后端、小样本、无长上下文与多轮恢复；当前已关闭真实调用 |
| 文档一致性 | 2026-09-21：D 编号对照表、编号治理规则、`docs/OPEN-QUESTIONS.md` 已建；三个活代码模块 AGENTS.md 已去除废弃架构术语 | 不等于产品功能实现 |

## 尚未实现

对应 `docs/features.json` 的 F10—F20，全部为 `planned`：

- F10/F11 统一 ModelBackend、能力协商、ReplayBackend、EvidenceGuard；
- F12/F14 Evidence & Artifact Ledger 与可重建 ContextBundle；
- F13 Tool Registry 的权限、副作用和 provenance 元数据；
- F15/F16 持久化 Workflow、节点 checkpoint、Human Gate、恢复和重放；
- F17 Document Resolver 与文档型取证（型号寻址、内容寻址去重、原文定位）；
- **F18 告警引擎——产品心脏，事件触发下的告警去重与关闭**；
- F19 多源采集与分歧呈现；
- F20 Replay/Eval Harness 与回归门禁；
- 外部采购草稿 Adapter 与幂等核对（Q-03）；
- 告警工作台 UI（F09，Q-07 未冻结）。

开发顺序即依赖顺序：F10→F11→F12→F13→F14→F15→F16→F17→F18，F19/F20 可并行收尾。

## 当前阻塞与待用户决定

未决项的正文、当前对策和冻结条件统一在 `docs/OPEN-QUESTIONS.md`，本文不复制。当前全部 8 项（Q-01 ～ Q-08）均未冻结。

其中**实际阻塞下一步实施**的只有两项：

| 编号 | 为何现在阻塞 |
|---|---|
| Q-02 | 决定第二个真实后端之前，能力协商的差异面无法定稿；但 `ReplayBackend` + 契约测试不受阻，可先做 |
| Q-01 | 决定首个真实供应源之前，`search_supplier_parts` / `get_supplier_offer` 的字段校准无法定稿；可先用 sample fixture 打通工作流 |

Q-03 ～ Q-08 目前不阻塞：对应能力本就排在后续切片。

另有一项等待用户裁决：**F08 是否按 2026-09-21 的 ①②③ 证据置为 `passing`**（④ 无外部依赖，属 `not_applicable`）。

## 下一实施顺序

1. 裁决 F08 状态（证据见上表），再对齐 `docs/features.json` 与新架构，拆分 ModelBackend、Context Compiler、Evidence Ledger 和 Durable Workflow 的 Feature；状态变更需用户授权。
2. 先实现 ReplayBackend + ModelBackend contract tests，形成无需真实模型的离线基线。
3. 建立最小 Evidence、Artifact、ContextBundle 与 node checkpoint 数据迁移。
4. 用一个“已有 shortage_snapshot → 样例供应证据 → 风险无法判断/发现 → proposal”工作流打通恢复。
5. 获得用户授权后接入首个真实模型和供应来源，执行 EV-39。

## 工作树保护

`src/infrastructure/database.py` 的未提交修改早于本次文档工作，未被修改、覆盖或回退。
