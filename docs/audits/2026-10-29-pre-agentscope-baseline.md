# AgentScope 迁移前代码基线

日期：2026-10-29。任务是保存当前代码、记录迁移计划并推送远端，不实施 AgentScope 接入或新的业务功能。

## 提交范围

现有分支为 `spec-v0.2-harness-reset`，远端为 `git@github.com:ruihanbabe/SupplyDemo.git`。开始时本地 HEAD 为 `b16c92628c27f41cff68c10a5b724c35bdcc72e2`，已领先远端 6 个提交；读取远端分支为 `441ced88e5255f65e41e4dc353278bdb3b5b75dd`。本轮保存当前工作树，再将既有提交与新基线一起推送，不改写历史、不合并到 main。

| 范围 | 处理 |
|---|---|
| Python SDK / Service 包、product_api 兼容入口及测试 | 纳入，记录当前 F53 A 目录归类现状；不声称 SDK 已独立或 AgentScope 已接入 |
| 前端、BFF、Pi sidecar 和协议文档 | 纳入当前版本，保留现有默认开关 |
| config/product、architecture、合成场景、Feature Contract 与业务/数据文档 | 纳入；测试夹具不等于真实业务验收 |
| app SQL 与 legacy Alembic 历史、维护入口 | 纳入文件；不执行任何迁移或数据删除 |
| 已有旧实现、旧资料和旧配置删除 | 按现有工作树保存；本轮不重新删除或恢复 |
| Python/Node 锁文件、环境模板和启动配置 | 保留可复现所需文件；真实环境文件不提交 |
| `.claude/settings.local.json` | 保留本地修改，不读取其配置正文、不修改、不暂存；它原本已被 Git 跟踪，忽略规则不会自动移除历史记录 |
| `.env`、前端本地环境、运行数据、日志、虚拟环境、node_modules、Next 构建产物 | 不提交 |
| `frontend/tsconfig.tsbuildinfo` | 本地保留；新增 `*.tsbuildinfo` 忽略规则，排除可重建缓存 |

本轮新增 [迁移 TODO](../../TODO-agentscope-migration.md)，后续按基线保存、SDK 适配验证、正式重构顺序推进。没有复制 AgentScope 源码、安装依赖、修改数据库或调用真实模型/渠道。

最终基线暂存范围为 464 个路径：197 个新增、54 个修改、213 个删除。检查时本地 HEAD 未变化，个人配置内容指纹与开始时一致；完整路径清单仅保存在本地验证目录，不额外复制到长期文档。

## 当前执行证据

| 检查 | 结果 |
|---|---|
| `make check` | 通过；compile、ruff 通过，281 passed、91 deselected、1 条既有 Starlette 弃用提示 |
| `make web-check` | 通过；前端边界检查、ESLint、Next.js 16.4.0 生产构建及构建内 TypeScript 检查通过 |
| 两个 Pi `.mjs` 文件的 `node --check` | 通过 |
| 配置与 features JSON | 7 个 JSON 解析通过，24 个 Feature ID 唯一 |
| `git diff --check` | 通过；最终暂存内容另行检查 |
| 新迁移 TODO 的中文标点检查 | 通过 |
| 常见密钥格式扫描 | 当前候选文件与既有 6 个待推送提交的 89 个新增历史 blob 无匹配；覆盖私钥、GitHub token、OpenAI 风格 key、AWS access key、JWT |
| 字面量凭据与带密码 URI 复核 | 唯一需复核项为测试中的 `opaque-never-send` 合成敏感输入，非真实凭据；环境模板中的本地开发密码保留示例标识 |
| SSH 远端身份 | GitHub 返回已认证身份 `ruihanbabe`，与远端仓库所有者一致 |

扫描是列明规则的检查，不是对全部历史和所有凭据格式的安全审计。此次审阅用于界定快照提交范围，不代表重新完成所有 Feature 的逐行代码审计或用户验收。

离线与前端完整日志保存在本地忽略目录 `.runtime/verification/pre-agentscope-migration-{check,web}-2026-10-29.txt`，不作为远端随附制品。

## 保留的限制

- 本轮未运行数据库集成、真实 Provider、embedding、渠道、浏览器端到端或业务库迁移。
- 历史集成失败和时钟诊断仍见 [实现摘要](2026-10-28-implementation-summary.md)，本轮没有重启排查、修改断言或宣称修复。
- 历史前端生产构建阻断在本轮未复现；本轮构建通过不构成其根因已修复的证据。
- 构建使用当前机器已有前端本地环境，不表示新机器环境、真实登录和生产部署已验收。
- F53 现有 SDK 到 Service 的登记过渡依赖仍保留；本轮保存现状，不继续职责拆分或把目录迁移当作 AgentScope 适配。

## 续接

基线已提交并推送至 `origin/spec-v0.2-harness-reset`，提交为 `18093823a6122e8a37e4a4e1cb8b306c2d39dab2`；推送后已通过 `git ls-remote` 独立核对一致。已有 6 个本地提交一并推送。远端 Actions API 本次返回该分支运行列表为空，不宣称远端 CI 通过。推送后本地仅保留 `.claude/settings.local.json` 原有未暂存修改；该文件未包含在基线提交中。

下一项是迁移 TODO 的阶段二：核对固定 AgentScope 版本及依赖，通过公开接口验证一条只读调查链。当前授权止于记录计划和保存远端基线；本轮不开始迁移实现。
