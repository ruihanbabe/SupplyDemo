"""F07 工具代理层：工具返回留档表 tool_record（spec §4.3、G5：每次响应随 Session 留档，Eval 读留档回放）。

主键 (run_id, record_id)；原始返回 raw 与交付数据 delivered 分开存（注入可区分）；只追加。
不授予 supplyagent_app 权限（归 F11）。依赖：0003_session（复用 0002 的 env_append_only 触发函数）。
"""
from alembic import op  # alembic 迁移操作

revision = "0004"  # 本迁移编号
down_revision = "0003"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖


# 功能：建留档表与只追加触发器。完成：主键、模拟日检查。
def upgrade():
    op.execute("""
CREATE TABLE tool_record (
    run_id         TEXT    NOT NULL,                   -- 运行号
    record_id      TEXT    NOT NULL,                   -- 记录号 rec-N
    case_id        TEXT,                               -- 案子
    day            INTEGER NOT NULL CHECK (day >= 0),  -- 模拟日
    tool           TEXT    NOT NULL,                   -- 工具名
    providers      JSONB   NOT NULL,                   -- 涉及的提供方
    args           JSONB   NOT NULL,                   -- 规范化参数
    raw            JSONB,                              -- 原始返回（校验、注入之前）
    delivered      JSONB,                              -- 交付数据（校验、注入之后）
    warnings       JSONB   NOT NULL,                   -- 警告
    from_cache_of  TEXT,                               -- 缓存命中时指向原记录
    PRIMARY KEY (run_id, record_id)
)""")  # 留档表
    op.execute("""CREATE TRIGGER tool_record_append_only BEFORE UPDATE OR DELETE ON tool_record
                  FOR EACH ROW EXECUTE FUNCTION env_append_only()""")  # 只追加


# 功能：回退。完成：删表。
def downgrade():
    op.execute("DROP TABLE tool_record")  # 删表
