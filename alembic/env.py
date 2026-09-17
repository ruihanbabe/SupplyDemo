"""Run migrations with a separately configured database owner connection."""
from sqlalchemy import create_engine, pool

from alembic import context
from infrastructure.database import database_url


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
