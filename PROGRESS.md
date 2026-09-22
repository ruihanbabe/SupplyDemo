# 当前进度

> **文档契约** · 类型：当前状态 SSOT · 读取：每次会话启动，全文
> 更新：实现、验证、阻塞或下一步变化时覆盖更新
> 独占：当前完成度、当前证据、阻塞与下一步
> 不收录：历史流水账、需求正文、设计理由；历史由 Git 保存

更新时间：2026-09-22。

## 当前结论

产品形态已于本日重定为**元件采购决策助手**（D01），需求与架构文档已按技术展示清单（`requirements.md` §2）整体重写。旧的「供应风险主动预警为主线」形态及其派生设计作废。

代码侧有两块可用资产：**确定性采购业务核**与**统一模型端口**。Graph 编排器、Worker、权限层、预警流、trace、eval 全部未建。

## 已有实现

| 能力 | 落点 | 验证状态 |
|---|---|---|
| 本地环境、PostgreSQL 16、Redis 7、Alembic 迁移 | `Makefile`、`compose.yaml`、`alembic/` | 已验证 |
| 只读原始 BOM → 规范化 → 入库 | `data/supplychain/`、`src/persistence/import_normalized.py` | 已验证 |
| 需求展开、候选处理、缺口计算、报价规则、MOQ/包装/阶梯价、版本化方案 | `src/procurement_core/`、`src/persistence/procurement.py` | 已验证 |
| FastAPI 包络与项目/BOM/需求/Run/缺口/方案端点 | `src/api/` | 代码与测试在，未按四层验收 |
| 统一 `ModelBackend` 端口、能力协商、云端后端、回放后端、录制 | `src/contracts/llm.py`、`src/infrastructure/llm.py`、`src/infrastructure/replay.py` | 已验证，含跨后端契约测试 |
| 对话入口与流式呈现、服务端收口的工具调用 | `src/api/routers/chat.py`、`frontend/`、`src/tools/` | 冒烟通过，未按四层验收 |

## 当前验证证据

| 范围 | 证据 |
|---|---|
| 静态契约 | 2026-09-22：`make compile` 通过；`make lint` All checks passed |
| 离线测试 | 2026-09-22：`make test` 125 passed / 22 deselected |
| 集成 | 2026-09-21：`make test-integration` 22 passed（真 PostgreSQL / Redis） |
| 模型后端 | 2026-09-22：真实 GLM 端到端两轮工具对话；录制后断网重放逐字重现，零花费 |

## 尚未实现

按 `requirements.md` 的技术点编号：

- **T01 / T02 / T03** Graph 编排器、Worker 名册、fan-out/fan-in、部分失败语义、RiskEvent 异步流——全部未建，是第一期主体
- **T04** 分层权限策略与 `explain`——未建
- **T05** 能力探测套件、本地部署后端——未建（端口与回放已就绪）
- **T06** MCP 客户端与进程内 skill 的并存——未建
- **T07** 树状调用轨迹、三类日志分离——未建
- **T08** Human Gate、审批绑定哈希、重启后仍等待——未建
- **T09** 父子预算与沿边扣除——未建
- **T10** eval 数据集、失败用例登记、回归门禁——未建（回放后端是前置件，已就绪）

## 文档体系状态

| 文档 | 状态 |
|---|---|
| `docs/product/requirements.md` | ✅ 已按新形态重写（207 行） |
| `ARCHITECTURE.md` | ✅ 已重写（200 行） |
| `DECISIONS.md` | ✅ 已重写，D01/D02/D05/D10 含义改变，新增 D20–D24（167 行） |
| `docs/OPEN-QUESTIONS.md` | ✅ 已重写，Q-02 废止 |
| `productinfo.md`、`docs/HANDOFF.md` | ✅ 已删除，内容并入上述文档 |
| `AGENTS.md`、`README.md`、`CODING_RULES.md`、`GLOSSARY.md` | ⏳ 待对齐 |
| `docs/features.json` | ⏳ 待按新形态重排队列 |
| `docs/product/acceptance-cases.md` | ⏳ 待重写（旧 EV 用例引用已废止的 FR/BR） |
| `docs/spec/*` | ⏳ 待检查：`interfaces.md` 需加 `RiskEvent` / `NodeResult`；`data-model.md` 的预警层随新形态调整；`ui-contract.md` 暂缓 |

## 阻塞

无技术阻塞。Q-01 / Q-03 / Q-04 / Q-05 未冻结，但第一期切片用 `sample` / `replay` 数据即可跑完前三层验证。

## 下一步

1. 对齐剩余文档（`AGENTS.md`、`README.md`、`CODING_RULES.md`、`GLOSSARY.md`）；
2. 按新形态重排 `docs/features.json`，旧 F 编号的处置需用户确认；
3. 重写 `docs/product/acceptance-cases.md`；
4. 然后才开工：Graph 编排器 → Worker 名册 → 第一期纵向切片。

Feature 状态只能由用户按 D14 授权流转，文档工作不修改 `state` 或 `evidence`。
