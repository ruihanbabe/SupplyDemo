"""Run preserved legacy migrations with an explicitly configured owner connection."""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from alembic import context


def database_url() -> str:
    """Read the product's synchronous maintenance URL without importing retired code."""
    load_dotenv(".env")
    url = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not url:
        raise RuntimeError("SUPPLYAGENT_PRODUCT_DATABASE_URL is required for legacy Alembic maintenance")
    parsed = make_url(url)
    if parsed.drivername == "postgresql":
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def configure(connection):
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=database_url(), literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        configure(supplied)
    else:
        engine = create_engine(database_url(), poolclass=pool.NullPool, hide_parameters=True)
        try:
            with engine.connect() as connection:
                configure(connection)
        finally:
            engine.dispose()
