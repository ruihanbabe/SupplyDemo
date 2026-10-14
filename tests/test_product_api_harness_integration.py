"""F42 在真实 Node 子进程、RLS 事务和 F40 SQL Trace 上的场景验证。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from product_api.harness import (
    Harness,
    HarnessContext,
    PiRuntimeAdapter,
    SqlTraceSink,
    ToolDefinition,
    ToolRegistry,
)
from product_api.trace import PostgresTraceRepository

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "pi_sidecar" / "f42_protocol_sidecar.mjs"


@pytest.fixture
def product_database_url() -> str:
    load_dotenv(".env")
    required = [
        "SUPPLYAGENT_PG_USER",
        "SUPPLYAGENT_PG_PASSWORD",
        "SUPPLYAGENT_PG_HOST",
        "SUPPLYAGENT_PG_PORT",
        "SUPPLYAGENT_PG_DB",
    ]
    if any(not os.getenv(name) for name in required):
        pytest.skip("本地 PostgreSQL 环境未配置")
    return "postgresql://{user}:{password}@{host}:{port}/{database}".format(
        user=os.environ["SUPPLYAGENT_PG_USER"],
        password=os.environ["SUPPLYAGENT_PG_PASSWORD"],
        host=os.environ["SUPPLYAGENT_PG_HOST"],
        port=os.environ["SUPPLYAGENT_PG_PORT"],
        database=os.environ["SUPPLYAGENT_PG_DB"],
    )


@pytest.mark.integration
def test_harness_persists_verified_node_turn_as_sql_trace(product_database_url: str) -> None:
    asyncio.run(_scenario(product_database_url))


async def _scenario(database_url: str) -> None:
    organization_id, actor_id = uuid4(), uuid4()
    await _insert_fixture(database_url, organization_id, actor_id)
    try:
        traces = PostgresTraceRepository()
        async with await AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("set local role supplyagent_api")
                await cursor.execute(
                    "select app.set_request_context(%s::uuid, %s::uuid)",
                    (actor_id, organization_id),
                )
            trace_run_id = await traces.begin_run(connection, "worker", None)
            harness = Harness(
                PiRuntimeAdapter(("node", str(SIDECAR))),
                ToolRegistry((ToolDefinition("supply.impact", lambda arguments: {"part": arguments["partNumber"]}),)),
            )
            result = await harness.execute(
                HarnessContext(organization_id, uuid4(), None, "验证受控 F42 turn", ("supply:PART-A",)),
                SqlTraceSink(connection, traces, trace_run_id, None),
            )
            assert result.status == "completed" and result.event_count == 3
            assert await traces.verify_chain(connection, trace_run_id)
            events = await traces.list_events(connection, trace_run_id, None)
            assert [event.event_type for event in events] == [
                "pi.turn_started",
                "pi.tool_call",
                "pi.turn_completed",
            ]
            assert all("part" not in event.metadata for event in events)
            assert await traces.verify_chain(connection, trace_run_id)
            await connection.rollback()
    finally:
        await _delete_fixture(database_url, organization_id)


async def _insert_fixture(database_url: str, organization_id: UUID, actor_id: UUID) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "insert into app.organization (id, slug, display_name) values (%s, %s, 'F42')",
                (organization_id, f"f42-{organization_id.hex[:12]}"),
            )
            await cursor.execute(
                "insert into app.organization_member (organization_id, user_id, role) values (%s, %s, 'organization_admin')",
                (organization_id, actor_id),
            )
        await connection.commit()


async def _delete_fixture(database_url: str, organization_id: UUID) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("delete from app.trace_event where organization_id = %s", (organization_id,))
            await cursor.execute("delete from app.trace_artifact where organization_id = %s", (organization_id,))
            await cursor.execute("delete from app.trace_run where organization_id = %s", (organization_id,))
            await cursor.execute("delete from app.organization where id = %s", (organization_id,))
        await connection.commit()
