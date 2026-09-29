"""F01 baseline checks: database URL handling and the migrated schema on real PostgreSQL."""
import pytest
from sqlalchemy import inspect, text

from alembic import command
from infrastructure.database import database_url

BASELINE = {"hw_part", "alembic_version"}


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
def test_baseline_has_only_hw_part_and_survives_repeat_and_downgrade(migrated):
    conn, config, schema = migrated
    assert set(inspect(conn).get_table_names(schema=schema)) == BASELINE
    command.upgrade(config, "head")
    assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
    command.downgrade(config, "base")
    assert inspect(conn).get_table_names(schema=schema) == ["alembic_version"]
    command.upgrade(config, "head")
    assert set(inspect(conn).get_table_names(schema=schema)) == BASELINE


@pytest.mark.integration
def test_app_role_can_only_read_hw_part(migrated):
    conn, _, schema = migrated
    for action, allowed in (("SELECT", True), ("INSERT", False), ("UPDATE", False),
                            ("DELETE", False), ("TRUNCATE", False)):
        assert conn.scalar(text("SELECT has_table_privilege('supplyagent_app', :t, :a)"),
                           {"t": f'"{schema}".hw_part', "a": action}) is allowed
