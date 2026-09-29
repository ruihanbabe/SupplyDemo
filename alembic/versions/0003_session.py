"""F06 Session：事件表、Artifact 表、依赖边表（spec §5.1 存储：三张表，全部只追加）。

- session_event：一次运行一条哈希链，主键 (run_id, seq)；去重键运行内唯一。
- artifact：(run_id, artifact_id, version) 唯一，内容 JSONB 不可变。
- artifact_dep：依赖边，指向另一个 Artifact 版本或一个外部数据版本（带按模拟日的有效期）。
三张表复用 0002 的 env_append_only() 触发函数禁止 UPDATE / DELETE；不授予 supplyagent_app 权限（归 F11）。
依赖：0002_env。被 session.store.PgSessionStore 使用。
"""
from alembic import op  # alembic 迁移操作

revision = "0003"  # 本迁移编号
down_revision = "0002"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖

TABLES = ("session_event", "artifact", "artifact_dep")  # 本迁移建的三张表


# 功能：建三张表与只追加触发器。依赖：0002 的触发函数。完成：主键、唯一、检查约束。
def upgrade():
    op.execute("""
CREATE TABLE session_event (
    run_id       TEXT    NOT NULL,                  -- 运行号
    seq          INTEGER NOT NULL CHECK (seq > 0),  -- 链上序号
    case_id      TEXT,                              -- 案子号（环境事件等可为空）
    day          INTEGER NOT NULL CHECK (day >= 0), -- 模拟日
    wall_time    TEXT    NOT NULL,                  -- 真实时间 ISO 字符串（不进哈希）
    actor        TEXT    NOT NULL,                  -- 执行者（Harness 标注）
    type         TEXT    NOT NULL,                  -- 事件类型
    payload      JSONB   NOT NULL,                  -- 内容
    entity_keys  JSONB   NOT NULL,                  -- 实体键 {类别: [键]}
    caused_by    INTEGER,                           -- 因果：同运行内的事件序号
    dedup_key    TEXT,                              -- 去重键
    meta         JSONB   NOT NULL,                  -- 元数据（代表链、污点等）
    prev_hash    TEXT    NOT NULL,                  -- 上一事件哈希
    hash         TEXT    NOT NULL,                  -- 本事件哈希
    PRIMARY KEY (run_id, seq),
    CONSTRAINT uq_session_event_dedup UNIQUE (run_id, dedup_key)
)""")  # 事件表
    op.execute("CREATE INDEX ix_session_event_case ON session_event (run_id, case_id, seq)")  # 按案子读
    op.execute("""
CREATE TABLE artifact (
    run_id        TEXT    NOT NULL,                      -- 运行号
    artifact_id   TEXT    NOT NULL,                      -- Artifact 标识
    version       INTEGER NOT NULL CHECK (version > 0),  -- 版本
    kind          TEXT    NOT NULL,                      -- 类型，如 SourcingQuote
    case_id       TEXT,                                  -- 所属案子
    content       JSONB   NOT NULL,                      -- 内容（不可变）
    content_hash  TEXT    NOT NULL,                      -- 内容哈希
    produced_seq  INTEGER NOT NULL,                      -- 产出事件序号
    day           INTEGER NOT NULL CHECK (day >= 0),     -- 产出模拟日
    meta          JSONB   NOT NULL,                      -- 元数据（污点标签等）
    PRIMARY KEY (run_id, artifact_id, version),
    FOREIGN KEY (run_id, produced_seq) REFERENCES session_event (run_id, seq)
)""")  # Artifact 表
    op.execute("""
CREATE TABLE artifact_dep (
    run_id           TEXT    NOT NULL,                                        -- 运行号
    artifact_id      TEXT    NOT NULL,                                        -- 依赖方
    version          INTEGER NOT NULL,                                        -- 依赖方版本
    dep_kind         TEXT    NOT NULL CHECK (dep_kind IN ('artifact', 'external')),  -- 边的类型
    target           TEXT    NOT NULL,                                        -- 被依赖的 Artifact ID 或外部引用
    target_version   TEXT    NOT NULL,                                        -- 被依赖的版本
    valid_until_day  INTEGER,                                                 -- 外部数据有效期（含当日）
    PRIMARY KEY (run_id, artifact_id, version, dep_kind, target),
    FOREIGN KEY (run_id, artifact_id, version) REFERENCES artifact (run_id, artifact_id, version)
)""")  # 依赖边表
    op.execute("CREATE INDEX ix_artifact_dep_target ON artifact_dep (run_id, dep_kind, target)")  # 反查下游
    for table in TABLES:  # 三张表都挂只追加触发器
        op.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
                       FOR EACH ROW EXECUTE FUNCTION env_append_only()""")  # 复用 0002 的触发函数


# 功能：回退本迁移。完成：按外键依赖逆序删表。
def downgrade():
    for table in reversed(TABLES):  # 先删依赖边，再删 Artifact，最后删事件
        op.execute(f"DROP TABLE {table}")  # 删表（触发器与索引随表删除）
