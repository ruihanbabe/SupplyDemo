"""v0.3 baseline: only the hardware part table and the application role.

The v0.1 schema and its follow-ups (old 0001–0006) are in git history. hw_part is derived
from the pinned KiCad files under data/hardware/raw and rebuilt by make build-parts.
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Roles are cluster-wide and can already exist in a different database.
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'supplyagent_app') THEN
            CREATE ROLE supplyagent_app NOLOGIN;
        END IF;
    END $$""")
    op.execute("""
CREATE TABLE hw_part (
    project_id       TEXT        NOT NULL,
    commit_sha       TEXT        NOT NULL,
    mpn              TEXT        NOT NULL,
    manufacturer     TEXT,
    description      TEXT,
    "values"         TEXT[]      NOT NULL,
    footprints       TEXT[]      NOT NULL,
    designators      TEXT[]      NOT NULL,
    quantity         INTEGER     NOT NULL,
    alternates       TEXT,
    CONSTRAINT uq_hw_part UNIQUE NULLS NOT DISTINCT (project_id, mpn, manufacturer),
    CONSTRAINT ck_hw_part_quantity CHECK (quantity = cardinality(designators) AND quantity > 0)
)
    """)
    op.execute("GRANT SELECT ON hw_part TO supplyagent_app")


def downgrade():
    # Keep the cluster-wide role: other databases may depend on it.
    op.execute("DROP TABLE hw_part")
