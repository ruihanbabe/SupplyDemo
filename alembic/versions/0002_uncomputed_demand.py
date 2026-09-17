"""Represent uncomputed demand as NULL (user-approved F03 contract correction)."""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE demand_line ALTER COLUMN required_qty DROP NOT NULL")
    op.execute("ALTER TABLE demand_line DROP CONSTRAINT ck_demand_line_resolved")
    op.execute("""ALTER TABLE demand_line ADD CONSTRAINT ck_demand_line_resolved CHECK (
        (component_id IS NOT NULL AND unresolved_reason IS NULL
         AND required_qty IS NOT NULL AND required_qty > 0)
        OR (component_id IS NULL AND unresolved_reason IS NOT NULL
            AND (required_qty IS NULL OR required_qty > 0)))""")


def downgrade():
    # Refuse downgrade if NULLs exist; never silently turn unknown quantity into zero.
    op.execute("ALTER TABLE demand_line ALTER COLUMN required_qty SET NOT NULL")
    op.execute("ALTER TABLE demand_line DROP CONSTRAINT ck_demand_line_resolved")
    op.execute("""ALTER TABLE demand_line ADD CONSTRAINT ck_demand_line_resolved CHECK (
        (component_id IS NOT NULL AND unresolved_reason IS NULL)
        OR (component_id IS NULL AND unresolved_reason IS NOT NULL))""")
