"""Hardware part table: one row per purchasable part number per project, derived from
the raw layer and rebuildable from it (data-model.md §14)."""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE TABLE hw_part (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    mpn              TEXT        NOT NULL,
    manufacturer     TEXT,
    description      TEXT,
    quantity         INTEGER     NOT NULL,
    alternates       TEXT,
    PRIMARY KEY (snapshot_id, mpn),
    CONSTRAINT ck_hw_part_quantity CHECK (quantity > 0)
)
    """)
    op.execute("GRANT SELECT ON hw_part TO supplyagent_app")


def downgrade():
    op.execute("DROP TABLE hw_part")
