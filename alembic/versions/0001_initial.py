"""Initial schema from docs/spec/data-model.md sections 3 through 7."""
import re
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

AUDIT_TABLES = (
    "run_state_event", "tool_call", "llm_call", "permission_decision",
    "shortage_snapshot", "metrics_snapshot", "operator_log",
)


def statements():
    text = Path(__file__).with_suffix(".sql").read_text()
    return [s.strip() for s in re.sub(r"--[^\n]*", "", text).split(";") if s.strip()]


def upgrade():
    for statement in statements():
        op.execute(statement)
    # Roles are cluster-wide and can already exist in a different database.
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'supplyagent_app') THEN
            CREATE ROLE supplyagent_app NOLOGIN;
        END IF;
    END $$""")
    tables = ", ".join(AUDIT_TABLES)
    op.execute(f"GRANT SELECT, INSERT ON {tables} TO supplyagent_app")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {tables} FROM supplyagent_app")
    op.execute("GRANT USAGE ON SEQUENCE run_state_event_event_id_seq TO supplyagent_app")


def downgrade():
    # Keep the cluster-wide role: other databases may depend on it.
    for statement in reversed(statements()):
        match = re.match(r"CREATE TABLE (\w+)", statement)
        if match:
            op.execute(f"DROP TABLE {match[1]}")
