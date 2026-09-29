"""F03 环境内核：环境快照与外部写入日志两张表（spec §4.6、§5.1）。

- env_snapshot：每个模拟日开盘存一次整状态（JSONB）+ SHA-256；主键 (run_id, day)，只写一次。
- env_write：外部写入只追加日志；主键 (run_id, seq)，幂等键在运行内唯一。
两张表都用触发器禁止 UPDATE / DELETE（只追加）；不授予 supplyagent_app 任何权限（Agent 侧权限归 F11）。
依赖：0001_baseline。被 simenv.store.PgEnvStore 使用。
"""
from alembic import op  # alembic 迁移操作

revision = "0002"  # 本迁移编号
down_revision = "0001"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖


# 功能：建两张表与只追加触发器。依赖：0001 已建库。完成：主键、唯一约束、非负日检查、禁止改删。
def upgrade():
    op.execute("""
CREATE TABLE env_snapshot (
    run_id           TEXT        NOT NULL,                   -- 运行编号
    day              INTEGER     NOT NULL CHECK (day >= 0),  -- 模拟日（0 = 初始状态）
    state            JSONB       NOT NULL,                   -- 整状态
    state_hash       TEXT        NOT NULL,                   -- 规范化 JSON 的 SHA-256
    forked_from_run  TEXT,                                   -- 分叉来源运行（非分叉为空）
    forked_from_day  INTEGER,                                -- 分叉来源日
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),     -- 真实写入时间
    PRIMARY KEY (run_id, day)
)""")  # 快照表
    op.execute("""
CREATE TABLE env_write (
    run_id           TEXT        NOT NULL,                   -- 运行编号
    seq              INTEGER     NOT NULL CHECK (seq > 0),   -- 运行内递增序号（重放顺序）
    day              INTEGER     NOT NULL CHECK (day >= 0),  -- 写入发生的模拟日
    kind             TEXT        NOT NULL,                   -- 写入种类，如 submit_po
    idempotency_key  TEXT        NOT NULL,                   -- 幂等键
    payload          JSONB       NOT NULL,                   -- 写入内容
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),     -- 真实写入时间
    PRIMARY KEY (run_id, seq),
    CONSTRAINT uq_env_write_key UNIQUE (run_id, idempotency_key)
)""")  # 写入日志表
    op.execute("""
CREATE FUNCTION env_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END $$""")  # 只追加触发函数：任何改删都报错
    for table in ("env_snapshot", "env_write"):  # 两张表都挂
        op.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
                       FOR EACH ROW EXECUTE FUNCTION env_append_only()""")  # 行级触发器


# 功能：回退本迁移。完成：删表（触发器随表删除）与触发函数。
def downgrade():
    op.execute("DROP TABLE env_write")  # 删写入日志表
    op.execute("DROP TABLE env_snapshot")  # 删快照表
    op.execute("DROP FUNCTION env_append_only()")  # 删触发函数
