# SupplyAgent —— 供应链运营 Agent MVP

> **文档契约** · 类型：入口层（面向人） · 读取：了解项目全貌时读；Codex 会话启动读一次即可，实现阶段不必回读
> 更新：项目定位、目标架构轮廓或文档索引变更时由 Claude 写入
> 独占：面向人的项目介绍与文档索引
> 不收录：任何权威事实——架构以 `ARCHITECTURE.md` 为准，需求以 `productinfo.md` 为准，状态以 `PROGRESS.md` 为准；本文只做简化概览与指路

面向供应链行业运营人员（不具备数据工程/SQL 技能）的对话式 Agent 系统。目标是在库存周转、缺货率、物流异常等场景下，辅助运营人员更快获取信息、发现问题、做出决策，并在人工审批下执行工单/通知/采购等动作。

## 项目状态

**当前处于架构设计阶段，尚无代码实现。** 本文档及其索引的其他文档记录的是目标架构与已拍板的设计决策，不代表已完成的功能。所有"已确认"字样指业务/架构判断已拍板，不代表已有代码或测试证据。

## 目标架构（设计中，尚未实现）

```text
用户 UI（桌面/Web 待定）
  → FastAPI（鉴权 + 路由分发；多租户方案待定，见 ARCHITECTURE.md 占位）
  → Supervisor Agent（意图路由 + 按复杂度编排 + 结构化对话 state 持久化）
      ├─ 简单查询：Query Agent（NL2SQL／MCP 外部查询）→ Summary
      └─ 分析类查询：Query → Detail Agent（多维度信息收集）
                          → Research Agent（推理与判断）
                          → Summary Agent（结构化业务报告）
                          → Action Agent（人工审批下创建工单／发送通知）
  → PermissionDecision（人工审批门）
  → 执行（ERP 草稿创建等写操作）
```

自主监控入口：Monitor 层（纯程序，不经 LLM）定时计算库存周转率、缺货率、物流异常等指标；越过阈值才生成 TriggerEvent 喂给 Supervisor，与用户交互走同一条权限链，不单独开一套触发逻辑。

Agent 行为（system prompt／模型／工具列表）以 YAML 配置管理，走 Git 版本控制；业务阈值（如库存告警线）存 PostgreSQL 的 `business_rule` 表，版本化并审计，与 agent 行为配置分开维护。

完整架构决策记录（决策＋备选方案＋否决理由）见 `ARCHITECTURE.md` / `DECISIONS.md`。

## 已确认的业务需求基础

FR-01～FR-09（提交需求、缺料计算、供应查询、候选核验、方案生成、审批、执行、定时扫描、故障恢复）与 BR-01～BR-11（缺口计算、候选选型、报价、审批版本绑定等业务规则），作为上述"分析类查询"深度路径（Detail→Research→Summary→Action）的执行内核被复用。条款正文见 `docs/product/requirements.md`，验收用例见 `docs/product/acceptance-cases.md`。原 FR-10（知识展示）已随 `DECISIONS.md` D12 移除 RAG 而删除，编号不回收（见 D16）。

5 个开源硬件项目的真实 BOM 已完成落库（121 条用料行、148 个候选型号、160 条分销商料号，元件总数量 420），构成身份与技术规格层的基础数据。一条用料行可对应多个厂商的等效型号，这是候选选型与替代料判断的数据基础。原始文件在 `data/supplychain/domdata/`（只读不可变），规范化产出与已知缺口见 `data/supplychain/normalized/projects.yaml`。**这批数据不含时间序列，尚不足以支撑库存周转率／缺货率等指标计算**，需要补充一批合成的时间序列数据（库存/出入库/在途事件）才能让 Monitor 层的阈值逻辑有东西可算。

## 可观测性优先级

当前开发优先级是把三层记录做扎实，而不是扩大数据规模：

- **Agent Trace**：每次 LLM 调用与工具调用的完整记录（输入、输出、evidence_ref、耗时、token、模型版本、升级路由的触发规则）
- **操作人员日志**：人工审批/驳回/改阈值等操作的留痕，谁在什么时候做了什么
- **开发日志**：架构决策记录（ADR）、YAML 配置变更（走 Git history）、`business_rule` 表变更审计

具体表结构待定，设计原则见 `DEVELOPMENT.md`。

## 运行

**尚无可运行的产品代码**，但开发环境已就位（本地，见 `DECISIONS.md` D18）。命令以 `Makefile` 为准：

```bash
make help
```

首次准备：`make setup` 建 `.venv` 并生成 `.env`，`make services-up` 起 PostgreSQL 16.6 与 Redis 7.4.2。环境变量见 `.env.example`，服务拓扑见 `compose.yaml`，依赖版本台账见 `requirement.txt`。`make run` 目前会明确报错退出——`src/` 下还没有应用入口。

## 文档索引

- Codex 入口与硬约束：`AGENTS.md`
- 当前系统架构：`ARCHITECTURE.md`
- 架构决策记录：`DECISIONS.md`
- 开发指南（命令、验证层级、Feature 编排契约）：`DEVELOPMENT.md`
- Codex 文档记录规则：`CODING_RULES.md`
- 当前进度、阻塞和下一步：`PROGRESS.md`
- Feature 清单与验证契约：`docs/features.json`（待建，机制见 `DEVELOPMENT.md`）
- 产品需求：`productinfo.md`（定位、范围、业务边界）
- 功能需求与业务规则：`docs/product/requirements.md`（FR-01～09 / BR-01～11 正文）
- 验收用例：`docs/product/acceptance-cases.md`（EV 正文 + FR→EV 追踪矩阵）
- 领域术语：`docs/product/GLOSSARY.md`
- 命令入口 / 服务拓扑 / 环境变量 / 依赖台账：`Makefile`、`compose.yaml`、`.env.example`、`requirement.txt`
- 数据说明：`data/supplychain/normalized/projects.yaml`（已落库的项目注册表与已知缺口）
- 设计初稿与讨论记录（**归档，不作为实现依据**）：`data/supplychain/`

本地测试、mock 数据和进程内测试不能证明真实模型、数据库、外部 API 已经验收。
