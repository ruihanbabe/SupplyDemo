"""F01 contract checks and real PostgreSQL permission/rollback verification."""
import importlib.util
import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from infrastructure.database import database_url

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/spec/data-model.md"
REVISION = ROOT / "alembic/versions/0001_initial.py"


def revision():
    spec = importlib.util.spec_from_file_location("initial", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Where the frozen initial contract ends and later, separately-migrated layers begin.
#: One document stays the single DDL source while test_frozen_migration_matches_contract
#: keeps asserting that 0001 never drifts.
EVIDENCE_SECTION = "## 8. \u2465 \u8bc1\u636e\u5c42"

#: Tables migrated after 0001. F12 took only the field-type half of the evidence layer.
MIGRATED_BEYOND_0001 = {"evidence", "evidence_field_locator", "hw_source_snapshot",
                        "hw_source_file", "hw_raw_sheet", "hw_raw_symbol"}


def _create_blocks(markdown):
    blocks = re.findall(r"```sql\n(.*?)```", markdown, re.DOTALL)
    return "\n".join(block for block in blocks if "CREATE TABLE" in block)


def frozen_ddl():
    """Only the layers 0001 froze; the evidence layer migrates separately."""
    return _create_blocks(SPEC.read_text().split(EVIDENCE_SECTION)[0])


def spec_ddl():
    return _create_blocks(SPEC.read_text())


def test_frozen_migration_matches_contract():
    # F03 has an explicitly approved ALTER migration; all other initial DDL stays frozen.
    def without_demand_line(ddl):
        return re.sub(r"CREATE TABLE demand_line .*?\n\);", "", ddl, flags=re.DOTALL)
    assert without_demand_line(REVISION.with_suffix(".sql").read_text()) == without_demand_line(frozen_ddl())
    assert len(re.findall(r"CREATE TABLE", frozen_ddl())) == 22
    # The evidence (F12), alerting (F18), runtime (F14/F15) and semantic (F21) layers
    # grow the contract; they migrate separately from 0001, but must not drift silently.
    assert len(re.findall(r"CREATE TABLE", spec_ddl())) == 39
    # 39 contracted, 28 migrated. The gap is registered in data-model.md §12;
    # when ⑥⑦⑧⑨ get migrations these two numbers converge and this test changes.


def test_environment_url_overrides_dotenv(monkeypatch):
    monkeypatch.setenv("SUPPLYAGENT_DATABASE_URL", "postgresql+asyncpg://test:secret@localhost/demo")
    url = database_url()
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "demo"
    assert "secret" not in str(url)


def test_invalid_url_does_not_disclose_credentials(monkeypatch):
    monkeypatch.setenv("SUPPLYAGENT_DATABASE_URL", "secret-invalid-value")
    with pytest.raises(ValueError, match="^SUPPLYAGENT_DATABASE_URL is invalid$"):
        database_url()


@pytest.mark.integration
def test_upgrade_repeat_and_downgrade(migrated):
    conn, config, schema = migrated
    # 0001's layers plus F12's two evidence tables. The rest of ⑥ (document locators,
    # official_document, document_resolution) migrates with F17, which owns the
    # addressing that produces those rows; ⑦⑧⑨ are contracted but not yet migrated by
    # design (data-model.md §12).
    expected = set(re.findall(r"CREATE TABLE (\w+)", frozen_ddl())) | MIGRATED_BEYOND_0001
    assert set(inspect(conn).get_table_names(schema=schema)) == expected | {"alembic_version"}
    command.upgrade(config, "head")
    assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
    for table in expected:
        for column in inspect(conn).get_columns(table, schema=schema):
            assert str(column["type"]) not in {"FLOAT", "DOUBLE PRECISION"}
    command.downgrade(config, "base")
    assert inspect(conn).get_table_names(schema=schema) == ["alembic_version"]
    command.upgrade(config, "head")
    assert set(inspect(conn).get_table_names(schema=schema)) == expected | {"alembic_version"}


@pytest.mark.integration
def test_app_audit_permissions_and_identity_insert(migrated):
    conn, _, schema = migrated
    conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO supplyagent_app'))
    run_id = uuid4()
    conn.execute(text("INSERT INTO run(run_id,trigger_kind,state) VALUES (:id,'user','created')"),
                 {"id": run_id})
    conn.execute(text("SET LOCAL ROLE supplyagent_app"))
    # Identity sequence USAGE must permit a real append, not just an ACL check.
    conn.execute(text("""INSERT INTO run_state_event(run_id,to_state,reason,evidence_ref)
                        VALUES (:id,'created','test','{}')"""), {"id": run_id})
    assert conn.scalar(text("SELECT count(*) FROM run_state_event")) == 1
    for table in revision().AUDIT_TABLES:
        for action in ("SELECT", "INSERT"):
            assert conn.scalar(text("SELECT has_table_privilege(current_user,:table,:action)"),
                               {"table": f"{schema}.{table}", "action": action})
        for sql in (f"UPDATE {table} SET occurred_at=now()" if table in
                    {"run_state_event", "tool_call", "llm_call", "operator_log"} else
                    f"UPDATE {table} SET snapshot_id=snapshot_id" if table in
                    {"shortage_snapshot", "metrics_snapshot"} else
                    f"UPDATE {table} SET decision_id=decision_id",
                    f"DELETE FROM {table}", f"TRUNCATE {table}"):
            with pytest.raises(DBAPIError) as error, conn.begin_nested():
                conn.execute(text(sql))
            assert error.value.orig.sqlstate == "42501"
    conn.execute(text("RESET ROLE"))


@pytest.mark.integration
def test_ddl_failure_is_atomic(migrated):
    conn, config, schema = migrated
    command.downgrade(config, "base")
    # A late collision must roll back all earlier CREATE TABLE statements.
    conn.execute(text("CREATE TABLE business_rule (sentinel INTEGER)"))
    with pytest.raises(DBAPIError), conn.begin_nested():
        command.upgrade(config, "head")
    assert set(inspect(conn).get_table_names(schema=schema)) == {"alembic_version", "business_rule"}
    assert conn.scalar(text("SELECT count(*) FROM alembic_version")) == 0
    conn.execute(text("DROP TABLE business_rule"))
    command.upgrade(config, "head")
    assert len(inspect(conn).get_table_names(schema=schema)) == 29  # 28 migrated + alembic_version


@pytest.mark.integration
def test_migrated_columns_and_constraints_match_current_contract(migrated):
    """Compare migrated schema with an independently created target from the spec.

    Scoped to the frozen layers: the later layers have no migration to compare.
    """
    conn, _, actual_schema = migrated
    expected_schema = "test_target_" + uuid4().hex
    conn.execute(text(f'CREATE SCHEMA "{expected_schema}"'))
    conn.execute(text(f'SET LOCAL search_path TO "{expected_schema}"'))
    for statement in re.sub(r"--[^\n]*", "", frozen_ddl()).split(";"):
        if statement.strip():
            conn.execute(text(statement))
    inspector = inspect(conn)
    for table in re.findall(r"CREATE TABLE (\w+)", frozen_ddl()):
        def columns(schema, table=table):
            return [(c["name"], str(c["type"]), c["nullable"], c["default"])
                    for c in inspector.get_columns(table, schema=schema)]
        assert columns(actual_schema) == columns(expected_schema), table
        assert inspector.get_check_constraints(table, schema=actual_schema) == inspector.get_check_constraints(
            table, schema=expected_schema), table
        assert inspector.get_unique_constraints(table, schema=actual_schema) == inspector.get_unique_constraints(
            table, schema=expected_schema), table
    conn.execute(text(f'SET LOCAL search_path TO "{actual_schema}"'))
