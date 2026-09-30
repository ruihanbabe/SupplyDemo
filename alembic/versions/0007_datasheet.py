"""F19 datasheet 知识库 A：版本表与页表（spec §6.1、§6.2）。

- datasheet_version：料号 × 版本（内容哈希）的元数据（来源 URL、标题、目录、取得日）；旧版本保留。
- datasheet_page：逐页 Markdown 文本，tsv 为生成列（'simple' 分词，不做词干，参数名与型号按原样匹配）+ GIN 索引。
两表只追加（复用 0002 的 env_append_only 触发函数）；授予 F11 两个只读角色 SELECT。
依赖：0006_roles。被 datasheets.store.PgDatasheetStore 使用。
"""
from alembic import op  # alembic 迁移操作

revision = "0007"  # 本迁移编号
down_revision = "0006"  # 上一个迁移
branch_labels = None  # 无分支
depends_on = None  # 无额外依赖


# 功能：建两表、索引、只追加触发器与授权。完成：页码为正、版本主键。
def upgrade():
    op.execute("""
CREATE TABLE datasheet_version (
    mpn          TEXT        NOT NULL,                  -- 料号
    version      TEXT        NOT NULL,                  -- 版本 = 内容哈希
    source_url   TEXT,                                  -- 来源（样例为 fixture:// 路径）
    title        TEXT,                                  -- 标题
    toc          JSONB       NOT NULL,                  -- 目录 [{title, page}]
    fetched_day  INTEGER     NOT NULL CHECK (fetched_day >= 0),  -- 取得的模拟日
    seq          BIGSERIAL,                             -- 入库顺序（最新版本 = 最大）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),    -- 真实写入时间
    PRIMARY KEY (mpn, version)
)""")  # 版本表
    op.execute("""
CREATE TABLE datasheet_page (
    mpn      TEXT    NOT NULL,                          -- 料号
    version  TEXT    NOT NULL,                          -- 版本
    page     INTEGER NOT NULL CHECK (page > 0),         -- 页码
    text     TEXT    NOT NULL,                          -- 页文本（Markdown）
    tsv      TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,  -- 全文索引列
    PRIMARY KEY (mpn, version, page),
    FOREIGN KEY (mpn, version) REFERENCES datasheet_version (mpn, version)
)""")  # 页表
    op.execute("CREATE INDEX ix_datasheet_page_tsv ON datasheet_page USING GIN (tsv)")  # 全文索引
    for table in ("datasheet_version", "datasheet_page"):  # 两表
        op.execute(f"""CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
                      FOR EACH ROW EXECUTE FUNCTION env_append_only()""")  # 只追加
        op.execute(f"GRANT SELECT ON {table} TO supplyagent_agent_ro, supplyagent_procurement")  # F11 只读角色


# 功能：回退。完成：先删页表再删版本表。
def downgrade():
    op.execute("DROP TABLE datasheet_page")  # 删页表
    op.execute("DROP TABLE datasheet_version")  # 删版本表
