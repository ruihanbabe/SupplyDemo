"""Real database fixtures; rolled back and checked for cleanup after each test."""
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from infrastructure.database import database_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated():
    engine = create_engine(database_url(), hide_parameters=True)
    schema = "test_f01_" + uuid4().hex
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                config = Config(str(ROOT / "alembic.ini"))
                config.attributes["connection"] = conn
                command.upgrade(config, "head")
                yield conn, config, schema
            finally:
                transaction.rollback()
        with engine.connect() as conn:
            assert not conn.scalar(text("SELECT 1 FROM pg_namespace WHERE nspname=:name"),
                                   {"name": schema})
    finally:
        engine.dispose()
