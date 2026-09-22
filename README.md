# SupplyAgent

**元件采购决策助手**：运营人员提出采购需求 → 多 Agent 并行核验候选元件 → 生成采购建议 → 过程中发现的风险进入预警流 → 人工审批后执行。

这是一个**技术展示项目**。产品形态服务于技术清单，不是反过来——要覆盖的技术点见 [requirements.md §2](docs/product/requirements.md)：多 Agent 架构与通信、并行与依赖、权限隔离、多后端形态、MCP、trace、人在环审批、token 预算、evals。

## 核心设计论点

一个项目里用了**两种通信模式**，并且能说清为什么这里同步、那里异步：

| 子场景 | 模式 |
|---|---|
| 采购核验 | 确定性 Graph，同步 fan-out / fan-in |
| 预警 | 事件驱动，异步 `RiskEvent` |
| 对话入口 | Supervisor 意图路由 |

并发单位是「元件 × 核验类型」的二维展开：3 个缺料元件 × 3 类核验 = 9 个并发节点，落在 3 个 Worker 上。**任一分支失败不得在汇合处被当作成功**——这是设计的核心，不是边角。

## 当前状态

已有：本地环境与 PostgreSQL/Redis、规范化 BOM 数据与迁移、确定性采购业务核、部分 FastAPI 端点、统一 `ModelBackend` 端口（云端 + 回放后端 + 能力协商）、对话入口与流式呈现。

未建：Graph 编排器、Worker 名册、权限层、预警流、trace、eval。

准确状态见 [PROGRESS.md](PROGRESS.md)。

## 文档入口

| 内容 | 文件 |
|---|---|
| 技术清单、产品形态、FR / BR、切片范围、数据现状 | [requirements.md](docs/product/requirements.md) |
| 模块、契约类型、依赖方向、不变量 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 决策与理由 | [DECISIONS.md](DECISIONS.md) |
| 未决项 | [OPEN-QUESTIONS.md](docs/OPEN-QUESTIONS.md) |
| 术语 | [GLOSSARY.md](docs/product/GLOSSARY.md) |
| 接口与数据契约 | [docs/spec/](docs/spec/) |
| Feature 队列 | [features.json](docs/features.json) |

## 本地命令

`Makefile` 是命令的唯一权威入口：

```bash
make help
make status
make check
make services-up
```

真实模型、外部供应商 API 或任何收费能力需要用户明确授权。
