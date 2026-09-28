"""Hardware design raw layer: KiCad sources kept verbatim, split only along the file's
own structure (file -> sheet -> symbol). No unified fields — those belong to a later
processing layer that derives from this one without changing it (data-model.md §13).

Append-only for the application role: a new upstream commit is a new snapshot.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = ("hw_raw_symbol", "hw_raw_sheet", "hw_source_file", "hw_source_snapshot")


def upgrade():
    op.execute("""
CREATE TABLE hw_source_snapshot (
    snapshot_id      UUID PRIMARY KEY,
    tenant_id        TEXT        NOT NULL DEFAULT 'default',
    project_id       TEXT        NOT NULL,
    repo             TEXT        NOT NULL,
    commit_sha       TEXT        NOT NULL,
    source_path      TEXT        NOT NULL,
    root_schematic   TEXT        NOT NULL,
    license          TEXT        NOT NULL,
    manifest_sha256  TEXT        NOT NULL,
    fetched_at       TIMESTAMPTZ NOT NULL,
    imported_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_hw_source_snapshot_project_commit UNIQUE (project_id, commit_sha)
);

CREATE TABLE hw_source_file (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    path             TEXT        NOT NULL,           
    sha256           TEXT        NOT NULL,
    git_blob         TEXT        NOT NULL,           
    byte_size        INTEGER     NOT NULL,
    content          TEXT        NOT NULL,           
    PRIMARY KEY (snapshot_id, path)
);

CREATE TABLE hw_raw_sheet (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    sheet_path       TEXT        NOT NULL,           
    parent_path      TEXT,                           
    file_path        TEXT        NOT NULL,           
    sheet_uuid       TEXT,
    properties       JSONB       NOT NULL,           
    PRIMARY KEY (snapshot_id, sheet_path)
);

CREATE TABLE hw_raw_symbol (
    snapshot_id      UUID        NOT NULL REFERENCES hw_source_snapshot(snapshot_id),
    sheet_path       TEXT        NOT NULL,
    symbol_uuid      TEXT        NOT NULL,
    file_path        TEXT        NOT NULL,
    reference        TEXT,                           
    reference_source TEXT        NOT NULL,
    lib_id           TEXT        NOT NULL,
    unit             INTEGER,
    attributes       JSONB       NOT NULL,           
    properties       JSONB       NOT NULL,           
    PRIMARY KEY (snapshot_id, sheet_path, symbol_uuid),
    CONSTRAINT ck_hw_raw_symbol_reference_source CHECK (reference_source IN ('instance', 'property'))
);
CREATE INDEX ix_hw_raw_symbol_properties ON hw_raw_symbol USING GIN (properties);
    """)
    op.execute("GRANT SELECT, INSERT ON hw_source_snapshot, hw_source_file, hw_raw_sheet, "
               "hw_raw_symbol TO supplyagent_app")
    op.execute("REVOKE UPDATE, DELETE ON hw_source_snapshot, hw_source_file, hw_raw_sheet, "
               "hw_raw_symbol FROM supplyagent_app")


def downgrade():
    for table in TABLES:
        op.execute(f"DROP TABLE {table}")
