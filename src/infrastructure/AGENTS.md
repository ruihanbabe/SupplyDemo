# Infrastructure 模块指引

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

提供 LLM、PostgreSQL、Redis、外部供应商 API 的可替换 Adapter 实现，向核心端口返回规范化结果。LLM 通过抽象 `ModelProvider` 接口接入，不写死具体厂商。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Infrastructure 行、`DEVELOPMENT.md`「环境」、`.env.example` 与 `compose.yaml`；数据库配置入口见 `database.py`。

## 不变量与 contract

- 凭据不进入代码和 Trace；只从环境变量 / `.env` 读取。
- 外部失败不能伪装成功；替换 Provider 不改变核心数据语义。
- MVP 不引入向量库 / RAG（Infrastructure 仅 LLM / PostgreSQL / Redis / 供应商 API）。
- 连接借用服务器、调用真实模型或外部供应商 API、或产生费用前，必须获得用户明确授权。

## 修改后验证

数据库配置改动执行 `make compile`、`make lint`、`make test`，真实连接验证执行 `make test-integration`；`make model-smoke` 等真实调用需用户明确授权，按 `DEVELOPMENT.md` 的四层验证（`DECISIONS.md` D14）补充 Provider 失败路径确认。
