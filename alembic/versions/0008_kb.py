"""F20 知识库 B：条目表与状态变更表（spec §5.10、§6.3）。

- kb_entry：替代关系 / 料号别名条目（内容、依赖的外部数据版本、来源案子、人工确认记录）；跨运行的长期记忆，不带 run_id。
- kb_status：条目状态变更（未验证种子 / 已确认 / 待重验 / 已标记 EOL / 已撤销），当前状态 = 最新一条。
两表只追加（复用 0002 的 env_append_only 触发函数）；授予 F11 两个只读角色 SELECT（Agent 侧只读，写入只经人工确认的代码路径）。
依赖：0007_datasheet。被 knowledge_base.kb.PgKBStore 使用。
"""
from alembic import op  # alembic 迁移操作

revision = "0008"  # 本迁移编号
down_revision = "0007"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖


# 功能：建两表、只追加触发器与授权。完成：条目号主键、状态序号递增。
def upgrade():
    op.execute("""
CREATE TABLE kb_entry (
    entry_id      TEXT        PRIMARY KEY,               -- 条目号 KB-N
    kind          TEXT        NOT NULL CHECK (kind IN ('substitution', 'alias')),  -- 种类
    body          JSONB       NOT NULL,                  -- 内容（原料、替代料、等级、适用范围 / 说法、料号）
    deps          JSONB       NOT NULL,                  -- 依赖的外部数据 [{ref, version}]
    source_case   TEXT,                                  -- 来源案子（种子为空）
    confirmation  JSONB,                                 -- 人工确认记录 {node, by, event_seq}（种子为空）
    created_day   INTEGER     NOT NULL CHECK (created_day >= 0),  -- 写入的模拟日
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()     -- 真实写入时间
)""")  # 条目表
    op.execute("""
CREATE TABLE kb_status (
    seq       BIGSERIAL   PRIMARY KEY,                   -- 变更序号
    entry_id  TEXT        NOT NULL REFERENCES kb_entry (entry_id),  -- 条目
    status    TEXT        NOT NULL,                      -- 新状态
    reason    TEXT        NOT NULL,                      -- 原因
    actor     TEXT        NOT NULL,                      -- 谁改的
    day       INTEGER     NOT NULL CHECK (day >= 0)      -- 模拟日
)""")  # 状态变更表
    for table in ("kb_entry", "kb_status"):  # 两表
        op.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
                      FOR EACH ROW EXECUTE FUNCTION env_append_only()""")  # 只追加
        op.execute(f"GRANT SELECT ON {table} TO supplyagent_agent_ro, supplyagent_procurement")  # 只读角色


# 功能：回退。完成：先删状态表再删条目表。
def downgrade():
    op.execute("DROP TABLE kb_status")  # 删状态表
    op.execute("DROP TABLE kb_entry")  # 删条目表
