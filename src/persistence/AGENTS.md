# Persistence 模块指引（占位 stub）

> **文档契约** · 类型：模块层 · 读取：进入本模块前读，全文（篇幅短）
> 更新：本模块接口、不变量或验证方式变更时由 Codex 写入
> 独占：本模块实现级不变量细节、修改前置阅读清单、修改后验证命令
> 不收录：顶层已有的职责/依赖/不变量摘要（见根 `ARCHITECTURE.md` 模块表，不得复述）、决策理由、当前状态

## 职责

拥有 PostgreSQL 与 Redis 的存储 contract：对话 state、Run / ToolCall / PermissionDecision 审计记录、metrics_snapshot 落 PostgreSQL；缓存 / 幂等去重 / 分布式锁走 Redis。

## 修改前

阅读本目录 `ARCHITECTURE.md`、仓库根 `ARCHITECTURE.md` 的 Persistence 行、`DECISIONS.md` D05 / D11、早期数据分层设计（待迁移整理）；代码与测试待建。

## 不变量与 contract

- 审计表只插入不更新。
- Redis 不承担恢复所必需的权威事实。
- 数据库产品不定义业务规则；业务阈值归 `business_rule` 表，行为配置归 YAML。
- 可观测性三层记录（Agent Trace / 操作人员日志 / 开发日志）的表结构必须在架构阶段定好，历史数据补不齐（见 `DECISIONS.md` D11）。

## 修改后验证

`Makefile` 已建（目标见 `make help`），本模块代码与测试待建。届时运行本模块对应的 `make` 目标；连真实 Redis / PostgreSQL 的 smoke 用独立命名空间隔离，按 `DEVELOPMENT.md` 的四层验证（`DECISIONS.md` D14）补充事务与 TTL / 删除行为确认。
