# AGENTS.md — 编码 Agent 入口

只写入口与规则；内容以编号引用 `spec.md`，不在此复制正文（spec §0.5）。

## 先读
1. `HANDOFF.md`：现在在哪、下一步。
2. `contracts/Fxx.md`：当前 Feature 的 Contract（做什么、不做什么、验收证据）。
3. `spec.md`：按 Contract 引用的节号定点读，不通读。
4. `features.json`：Feature 清单与状态（spec §11）。

## 命令（唯一实现在 `Makefile`，`make help` 可列全）
| 命令 | 用途 |
|---|---|
| `make setup` / `make status` | 建虚拟环境、装依赖 / 查环境状态 |
| `make check` | 离线门禁：compile + lint + 离线测试 |
| `make services-up` / `make services-smoke` / `make services-down` | 起停本地 PostgreSQL + Redis，并做连通性冒烟 |
| `make migrate` | alembic 升级到 head |
| `make test-integration` | 连本地真实服务的集成测试（需先 `services-up`） |
| `make build-parts` | 从 `data/hardware/raw` 原理图重建 `hw_part`（先做 MANIFEST 校验） |
| `make fetch-hardware` / `make prune-hardware` | 联网拉取原理图 / 裁剪非电子件（改动原始层，需用户同意） |
| `make freeze` | 把虚拟环境固化为 `requirements.lock.txt` |

Makefile 约定：目标要么真的做事，要么明确报"尚未具备"，不得静默成功。

## 硬约束
- 全局边界 G1–G8（spec §0.4）约束所有代码，尤其：凭证不进模型上下文（G1）、写操作需审批令牌（G2）、数值不由 LLM 生成（G4）、业务参数不硬编码（G7）、不碰排产（G8）。
- 外部调用（G5）：分销商 API 不批量查；付费模型调用、批量真实调用须先获用户授权。
- 原始层 `data/hardware/raw/**` 只读，由 MANIFEST 哈希校验；`hw_part` 是派生表，只能由 `make build-parts` 重建。
- 数据库 schema 只通过 alembic 迁移改动；新表归属见各 Feature Contract。
- 新组件须通过准入四问（spec §0.3）并带消融开关。

## 协作规则
- Feature 开工前须有用户确认的 Contract；实现只在出现设计分歧时提问；完成后交运行证据，由用户判定验收（spec §0.5、§11）。
- 只做 Contract 范围内的事；额外想法先问，不擅自扩大范围。
- spec 已定策略不删、不降级；砍减只指推迟（spec §11）。
- 信息只写进单一权威文档，别处只留编号引用。
- 修复同一失败超过 3 次仍不通过，就停下来报告，不绕过检查（不删测试、不加 skip、不降阈值）。
- 提交前 `make check` 必须全绿。
