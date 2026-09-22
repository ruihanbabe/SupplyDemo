# 采购平台 API 与 MCP 接入调研（已提升，正文已迁出）

> ⚠️ 本文中的 `Dxx` 是 **2026-09-21 架构重构前的旧编号**，与现行 `DECISIONS.md` 不对应。对照表见 `DECISIONS.md`「附录 · 旧编号对照」。

> **文档契约** · 类型：归档 · 读取：**不读**。本文已无正文，按下表直接去新位置
> 更新：冻结。不再接受任何内容写入
> 独占：无
> 不收录：一切正文。原内容已按 `DECISIONS.md` D15 迁至下表各处

本文原为接入调研初稿（核对日期 2026-09-09）。2026-09-14 正文已全部迁出，此处只留去向对照。**完整原文见 Git 历史**（`git show 5eb4dbc:data/supplychain/PROCUREMENT-API-MCP.md`）。

| 原章节 | 现正文位置 |
|---|---|
| §1 采购数据来源比较 | `docs/research/procurement-platforms.md` §1 |
| §2 ERP 是什么，接入复杂度在哪里 | `docs/research/procurement-platforms.md` §2 |
| §3 MCP 封装边界与候选工具表 | `docs/spec/interfaces.md`（工具 schema、`ToolResult` 信封、错误模型） |
| §4 实现前还缺哪些信息 | `docs/research/procurement-platforms.md` §3；其中已决项见 `DECISIONS.md` D13，待办见 `PROGRESS.md` 任务看板 |
| §5 接入完成的证据 | `docs/product/acceptance-cases.md` §4 |
| 官方来源 [D1]～[P2] | `docs/research/procurement-platforms.md` §4 |

原候选工具 `search_component_documents` 已随 `DECISIONS.md` D12（MVP 移除 RAG）删除，未迁入 `interfaces.md`。
