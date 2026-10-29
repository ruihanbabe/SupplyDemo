# AGENTS.md — 编码 Agent 入口

## 先读
1. `HANDOFF.md`：当前状态与下一步。
2. `features.json` 与当前 `contracts/Fxx.md`：Feature 状态、范围及验收。
3. 按任务读取：业务规则/策略变更查 `business-logic.md` 的 BL 编号；字段、交接、状态、来源变更查 `data-contract.md` 的 DC 编号。
4. 产品目标和架构边界查 `spec.md` 对应章节，不通读历史材料。

## 命令

唯一实现在 `Makefile`，`make help` 列全。`make check` 是离线门禁；各 Feature 的集成目标说明是否先执行迁移，未授权业务库变更时使用克隆库目标。

- `make setup` / `make status`：环境装配/状态。
- `make services-up` / `make services-smoke` / `make services-down`：本地依赖。
- `make migrate`：legacy public schema 的 Alembic 维护；不管理 app 表。
- 产品 `app` schema 只经 Feature 的 `*-migrate` 目标应用，具体清单先确认。

## 硬约束与协作

- 全局边界 G1–G8 见 spec §0.3；密钥不进模型、数值不由模型签发、外部动作需受控授权、不排产。
- 外部付费模型、批量真实查询及渠道调用需按 G5 获得授权，不以测试或清理名义调用。
- legacy public 与产品 app 的迁移历史分开保留；不得重排历史、交叉管理同一表或恢复已删除旧实现。
- Feature 先确认 Contract；一次只激活一个实施单元。新增组件须说明必要性、替代、成本与验证，并保留消融路径。
- 产品范围和架构由 spec 管理；业务规则唯一正文在 business-logic，数据约定在 data-contract，Contract 只保留 Feature 边界与验收；别处留编号引用。
- 完成后交可执行证据，验收由用户决定。暂缓功能保留目标，不把未完成删除为已完成。
- 保留用户已有改动与暂存状态，不触碰 `.claude/settings.local.json`。涉及数据移除先备份、克隆验证，再执行已批准清单；发现新依赖或非空数据即停止。
- 同一失败超过 3 次仍不通过，报告原因；不删测试、不加 skip、不降断言来变绿。提交前 make check 必须通过。
