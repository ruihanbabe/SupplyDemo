"""F17 可观测计量：metering 表（spec §5.11：数据存本地 Postgres，与事件日志用事件序号、案子号关联）。

每次 LLM / 工具调用一行，主键 (run_id, event_seq) 指向对应的 llm_call / tool_call 事件；真实耗时只存这里。
只追加；不授予 supplyagent_app 权限（归 F11）。依赖：0004_tool_record。
"""
from alembic import op  # alembic 迁移操作

revision = "0005"  # 本迁移编号
down_revision = "0004"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖


# 功能：建计量表与只追加触发器。完成：外键指向事件、类型检查、按真实日期查的索引（配额面板与重建）。
def upgrade():
    op.execute("""
CREATE TABLE metering (
    run_id         TEXT    NOT NULL,                              -- 运行号
    event_seq      INTEGER NOT NULL,                              -- 对应事件序号
    kind           TEXT    NOT NULL CHECK (kind IN ('llm', 'tool')),  -- 类型
    case_id        TEXT,                                          -- 案子
    day            INTEGER NOT NULL,                              -- 模拟日
    wall_date      TEXT    NOT NULL,                              -- 真实 UTC 日期（配额按它）
    name           TEXT    NOT NULL,                              -- 端点名或工具名
    model          TEXT,                                          -- 模型 ID（LLM）
    input_tokens   INTEGER NOT NULL,                              -- 输入 token
    output_tokens  INTEGER NOT NULL,                              -- 输出 token
    latency_ms     INTEGER NOT NULL,                              -- 真实耗时
    cache_hit      BOOLEAN NOT NULL,                              -- 是否命中缓存
    quota_used     JSONB   NOT NULL,                              -- 实际扣了配额的提供方
    error_type     TEXT,                                          -- 错误类型
    repair         BOOLEAN NOT NULL,                              -- 是否修复轮
    fallback       BOOLEAN NOT NULL,                              -- 是否降级
    PRIMARY KEY (run_id, event_seq),
    FOREIGN KEY (run_id, event_seq) REFERENCES session_event (run_id, seq)
)""")  # 计量表
    op.execute("CREATE INDEX ix_metering_date ON metering (wall_date)")  # 按日期汇总
    op.execute("""CREATE TRIGGER metering_append_only BEFORE UPDATE OR DELETE ON metering
                  FOR EACH ROW EXECUTE FUNCTION env_append_only()""")  # 只追加


# 功能：回退。完成：删表。
def downgrade():
    op.execute("DROP TABLE metering")  # 删表
