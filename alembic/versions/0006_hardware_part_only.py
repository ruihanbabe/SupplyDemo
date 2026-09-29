"""Hardware knowledge in one table: the raw layer leaves the database.

The pinned KiCad files stay on disk under data/hardware/raw (git-tracked, MANIFEST-checked);
only hw_part is loaded, straight from those files (data-model.md §13).
"""
import importlib.util
from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

RAW_TABLES = ("hw_raw_symbol", "hw_raw_sheet", "hw_source_file", "hw_source_snapshot")


def upgrade():
    op.execute("DROP TABLE hw_part")
    for table in RAW_TABLES:
        op.execute(f"DROP TABLE {table}")
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


def _revision(filename):
    # Revision files start with a digit, so they are loaded by path, not imported by name.
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def downgrade():
    # Back to 0005's empty tables; their rows came from files and are rebuilt, not restored.
    op.execute("DROP TABLE hw_part")
    _revision("0004_hardware_raw.py").upgrade()
    _revision("0005_hardware_part.py").upgrade()
