"""F34 accounting through a real PostgreSQL/RLS tenant transaction."""

from __future__ import annotations

import asyncio
import os
import time
from uuid import UUID, uuid4

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from product_api.gateway import (
    CapabilitySigner,
    ModelCapability,
    ModelGateway,
    PostgresModelAccounting,
    ReplayChatProvider,
    create_model_gateway_app,
)
from product_api.tenant import PostgresTenantRepository


@pytest.fixture
def product_database_url() -> str:
    load_dotenv(".env")
    required = [
        "SUPPLYAGENT_PG_USER", "SUPPLYAGENT_PG_PASSWORD", "SUPPLYAGENT_PG_HOST",
        "SUPPLYAGENT_PG_PORT", "SUPPLYAGENT_PG_DB",
    ]
    if any(not os.getenv(name) for name in required):
        pytest.skip("本地 PostgreSQL 环境未配置")
    return "postgresql://{user}:{password}@{host}:{port}/{database}".format(
        user=os.environ["SUPPLYAGENT_PG_USER"], password=os.environ["SUPPLYAGENT_PG_PASSWORD"],
        host=os.environ["SUPPLYAGENT_PG_HOST"], port=os.environ["SUPPLYAGENT_PG_PORT"],
        database=os.environ["SUPPLYAGENT_PG_DB"],
    )


@pytest.mark.integration
def test_gateway_reserves_and_settles_in_real_rls_database(product_database_url: str) -> None:
    asyncio.run(_scenario(product_database_url))


async def _scenario(database_url: str) -> None:
    organization_id, actor_id, policy_id = uuid4(), uuid4(), uuid4()
    await _insert_fixture(database_url, organization_id, actor_id, policy_id)
    try:
        tenants = PostgresTenantRepository(database_url)

        async def begin_trace_run(connection: AsyncConnection[dict[str, object]]) -> UUID:
            async with connection.cursor() as cursor:
                await cursor.execute("select app.begin_trace_run('worker',null) as id")
                row = await cursor.fetchone()
                assert row is not None
                return row["id"]

        trace_run_id = await tenants.in_organization(actor_id, organization_id, begin_trace_run)
        signer = CapabilitySigner(b"g" * 32)
        scope = ModelCapability(actor_id, organization_id, trace_run_id, policy_id, int(time.time()) + 60)
        token = signer.issue(scope)
        app = create_model_gateway_app(
            ModelGateway(signer, PostgresModelAccounting(tenants), {"replay": ReplayChatProvider()})
        )
        payload = {"model": "ignored", "messages": [{"role": "user", "content": "task"}], "stream": True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.post(
                "/v1/chat/completions", json=payload,
                headers={"authorization": f"Bearer {token}", "x-supplyagent-call-key": "run-call-1"},
            )
            assert response.status_code == 200 and "data: [DONE]" in response.text
            duplicate = await client.post(
                "/v1/chat/completions", json=payload,
                headers={"authorization": f"Bearer {token}", "x-supplyagent-call-key": "run-call-1"},
            )
            assert duplicate.status_code != 200

        async def inspect(connection: AsyncConnection[dict[str, object]]) -> tuple[int, int]:
            async with connection.cursor() as cursor:
                await cursor.execute("select count(*) as n from app.model_usage where run_id=%s and outcome='success'", (trace_run_id,))
                usage = (await cursor.fetchone())["n"]
                await cursor.execute("select count(*) as n from app.model_reservation where run_id=%s and settled", (trace_run_id,))
                settled = (await cursor.fetchone())["n"]
                return usage, settled

        assert await tenants.in_organization(actor_id, organization_id, inspect) == (1, 1)
    finally:
        await _remove_fixture(database_url, organization_id)


async def _insert_fixture(database_url: str, organization_id: UUID, actor_id: UUID, policy_id: UUID) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "insert into app.organization(id,slug,display_name) values (%s,%s,'F34')",
                (organization_id, f"f34-{organization_id.hex[:12]}"),
            )
            await cursor.execute(
                "insert into app.organization_member(organization_id,user_id,role) values (%s,%s,'organization_admin')",
                (organization_id, actor_id),
            )
            await cursor.execute(
                "insert into app.model_policy(id,organization_id,provider,model_id,daily_budget_micros,max_call_cost_micros) values (%s,%s,'replay','fixed-v1',1000,600)",
                (policy_id, organization_id),
            )
        await connection.commit()


async def _remove_fixture(database_url: str, organization_id: UUID) -> None:
    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.cursor() as cursor:
            for table in ("model_usage", "model_reservation", "model_policy", "trace_run", "organization"):
                await cursor.execute(f"delete from app.{table} where organization_id=%s" if table != "organization" else "delete from app.organization where id=%s", (organization_id,))
        await connection.commit()
