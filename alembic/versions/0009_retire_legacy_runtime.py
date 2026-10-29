"""Retire approved empty legacy tables; preserve hardware and migration history."""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TABLES = (
    "artifact_dep", "artifact", "metering", "tool_record", "session_event",
    "datasheet_page", "datasheet_version", "kb_status", "kb_entry", "env_write", "env_snapshot",
)


def upgrade():
    # Lock all targets before checking emptiness so concurrent writes cannot be lost.
    for name in TABLES:
        op.execute(f'LOCK TABLE public."{name}" IN ACCESS EXCLUSIVE MODE')
    for name in TABLES:
        if op.get_bind().exec_driver_sql(f'SELECT EXISTS(SELECT 1 FROM public."{name}")').scalar():
            raise RuntimeError(f"Refusing to retire nonempty legacy table: {name}")
    for name in TABLES:
        op.execute(f'DROP TABLE public."{name}" RESTRICT')
    op.execute("DROP FUNCTION public.env_append_only() RESTRICT")


def downgrade():
    raise RuntimeError("Restore the verified pre-cleanup backup; do not fabricate legacy history")
