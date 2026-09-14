# SupplyAgent Codex 入口

> **文档契约** · 类型：入口层 · 读取：每次会话启动必读，全文
> 更新：全局硬约束或模块划分变更时由 Claude 写入；Codex 只读不写
> 独占：Codex 全局行为硬约束、新会话启动流程、顶层文档与模块入口索引
> 不收录：架构事实、决策理由、需求条款、当前状态、命令实现（各自见索引指向的文档）

面向供应链运营人员的对话式 Agent 系统 MVP，用于验证 workflow 与 Agent 混合架构、多 Worker 编排与可审计 Harness 工程能力。当前处于需求/架构设计阶段，尚无产品代码（见 `PROGRESS.md`）。

## 新会话启动

1. 阅读本文件、`README.md`、`PROGRESS.md`；`DECISIONS.md` 只浏览决策标题（`grep '^## ' DECISIONS.md`）建立索引，具体条目按需定点读取，不在启动阶段通读全文。
2. 运行 `git status --short --branch`，保护已有未提交修改（仓库尚未初始化时先确认是否需要初始化，不擅自创建）。
3. 环境与依赖尚未定稿，标准命令（见下）待 `Makefile` 建立后启用；当前先确认借用服务器的服务可用性（对应 `PROGRESS.md` T01）。
4. 根据任务确定所属模块（参照 `ARCHITECTURE.md` 的一级逻辑模块表），先读该目录的局部 `AGENTS.md`（占位 stub 已建，见下方"模块入口"）；stub 内容较薄时以 `ARCHITECTURE.md` 对应模块行为准。
5. 不默认扫描整个仓库或全部 Markdown；只读取与当前任务有关的文档和模块。

## 标准命令入口

```bash
make setup   # 待建
make status  # 待建
make check   # 待建
make run     # 待建
make health  # 待建
```

当前项目尚无代码仓库，`Makefile`、依赖锁文件、`.env.example`、`compose.yaml` 均待建（进度见 `PROGRESS.md`）。这些文件建立后，本节应替换为真实命令；Markdown 不能证明当前依赖、服务或外部系统状态，以 `Makefile` 与实际运行结果为准。

## 全局硬约束

- Feature 选择与并行边界：完整规则见 `DEVELOPMENT.md`「Feature 选择顺序与并行边界」，Feature 清单为 `docs/features.json`（待建）；清单建立前默认一次只推进一个明确范围的任务，不擅自顺带重构无关模块。
- 只读取和修改当前任务需要的文件，不做相邻重构；保留用户已有改动。
- **业务阈值与供应商特定映射不得硬编码进 Worker 代码或 prompt**：库存告警线等可变业务参数只能来自 PostgreSQL 的 `business_rule` 表（见 `DECISIONS.md` D07）；供应商字段映射等实现细节归 Tools（Skill/MCP）层，不得散落进 Query/Detail/Research 等 Worker 的推理逻辑里。
- 读取 `DECISIONS.md`、`ARCHITECTURE.md`、`productinfo.md` 时按标题/章节号定点读取，不通读全文：`DECISIONS.md` 各条目已标注 `<!-- id: Dxx -->`，可用 `grep` 定位；`productinfo.md` 采用编号章节（`## N.`），按需只读对应节。仅当怀疑"决策理解有误"且定点读取仍无法确认时，才临时读整份文件排查，排查完不得把全文留在长期上下文。
- 每份文档开头的**文档契约块**（`> **文档契约**` 四行）规定了该文档的读取时机、更新时机、独占信息与不收录信息；写入任何信息前先按该块判断归属，规则见 `CODING_RULES.md`。同一信息只在其 SSOT 文档保留正文，其他文档只写编号引用或一句话指针。
- `data/supplychain/` 下的设计初稿与讨论记录为**归档**，不得作为实现依据，除非用户明确要求否则不读入。
- Review 或诊断任务默认只报告，不自动修改。
- 不提交 `.env`、凭据、API Key、未脱敏的供应商报价数据或完整 Provider payload。
- **`data/supplychain/domdata/` 是原始数据层，只读，任何情况下不得修改、清洗、重排或补字段**（文件已设 `chmod 444`，完整性由同目录 `MANIFEST.json` 的 SHA-256 保证）。需要修正数据时改 `normalize_domdata.py` 的转换逻辑并重跑，产出落在 `data/supplychain/normalized/`；发现原始数据本身有误，报告给用户，不自行订正。
- 连接借用服务器、调用真实模型或外部供应商 API、或产生费用前，必须获得用户明确授权。
- 模型不得绕过 Tools（Skill/MCP）层直接调用外部系统；写操作（工单创建、通知发送、采购草稿等）不得绕过 `PermissionDecision` 审批门交付（见 `DECISIONS.md` D02、D03）。
- 只报告当前环境实际执行成功的验证；mock、语法编译和历史结果不是完整验收。
- 完成以外部验证证据为准，不以自评或"代码写完"代替；必需验证被阻塞时如实报告阻塞原因，并同步更新 `PROGRESS.md` 对应任务。
- 跨模块修改前必须遵守 `ARCHITECTURE.md` 定义的边界、数据所有权和依赖方向；稳定、可客观检测的约束应有自动检查，失败信息需说明何处违约、为何、如何修复。
- 重复、高风险且可客观检测的审查问题应提升为自动检查；不强行自动化纯审美意见。
- 过时历史不留在工作树；仅在用户明确要求时从 Git 历史恢复。
- 验证建议分级递进（语法/类型检查 → lint → 单元测试 → 模块/集成测试），每级自动修复尝试上限 3 次、四级独立计数，达到上限须停止并如实报告，不得跳级掩盖失败；具体工具链待 `DEVELOPMENT.md` 定稿。修复动作若改变函数签名或接口，须回退重跑更早的验证级别，不由模型自行判断是否需要回退。
- 遇到自身能力/权限不匹配、或验证反复失败超出上限的情况，必须停止并向用户说明阻塞原因，不无限重试、不静默放弃、不自行决定绕过约束；结构化上报机制的具体格式待后续设计。

## Claude 与 Codex 的分工

Codex 是代码实现的主力；Claude（在文档协作对话中）负责本套治理文档（`README.md` / `ARCHITECTURE.md` / `DECISIONS.md` / `PROGRESS.md` / `requirements.md` / 本文件等）的维护与迭代，不直接向代码仓库写入实现代码。Codex 应把这套文档作为需求与约束的权威来源；文档之间出现冲突时，以 `DECISIONS.md` 中日期最新的相关条目为准。

## 顶层文档与模块入口

- 系统架构、模块边界和依赖方向：`ARCHITECTURE.md`
- 产品需求与范围：`productinfo.md`（整合版，含定位/用户/主路径/MVP 范围/非范围/回答结构/业务边界/能力矩阵/存储分工）
- 当前设计约束：`DECISIONS.md`
- 当前状态、阻塞和下一步：`PROGRESS.md`
- 开发命令、验证层级与代码规范：`DEVELOPMENT.md`（仓库根目录）
- Codex 文档记录规则：`CODING_RULES.md`
- Feature 清单与三层验证契约：`docs/features.json`（待建，机制见 `DEVELOPMENT.md`；建立前用 `PROGRESS.md` 任务看板过渡）

进入以下模块前，先读取其局部 `AGENTS.md`（占位 stub 已建，职责与依赖以 `ARCHITECTURE.md` 一级逻辑模块表为准，细化随代码进行）：

- API（鉴权与路由）：`src/api/AGENTS.md`
- Supervisor（意图路由与复杂度判断）：`src/supervisor/AGENTS.md`
- Query / Detail / Research / Summary / Action Worker：`src/workers/*/AGENTS.md`
- 采购业务核（缺口计算、状态机等确定性规则）：`src/procurement_core/AGENTS.md`
- Monitor（指标计算与 TriggerEvent）：`src/monitor/AGENTS.md`
- Runtime 契约层：`src/contracts/AGENTS.md`
- Tools（Skill/MCP 封装）：`src/tools/AGENTS.md`
- Persistence / Infrastructure：`src/persistence/AGENTS.md`、`src/infrastructure/AGENTS.md`

边界不明确时先在 `ARCHITECTURE.md` 更新模块划分，再动代码或局部 `AGENTS.md`，不擅自创建未在 `ARCHITECTURE.md` 出现的新模块。
