"""Per-request database scope.

One request is one transaction: the endpoint either commits everything it wrote or
leaves nothing behind. procurement_core's repository.atomic() nests inside this.
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from infrastructure.database import database_url
from persistence.procurement import ProcurementRepository


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    # hide_parameters keeps bound values out of SQLAlchemy logs and tracebacks.
    return create_engine(database_url(), hide_parameters=True, pool_pre_ping=True)


def get_repository() -> Iterator[ProcurementRepository]:
    with get_engine().connect() as connection, connection.begin():
        yield ProcurementRepository(connection)


#: Shared alias so routers declare the dependency in the annotation rather than in a
#: default value, which would evaluate Depends() once at import time (ruff B008).
Repo = Annotated[ProcurementRepository, Depends(get_repository)]
