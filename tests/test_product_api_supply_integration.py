"""F39 与本地 PostgreSQL 的端到端验收；fixture 在结束时删除。"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from psycopg import AsyncConnection

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.supply import PostgresSupplyRepository
from product_api.tenant import PostgresTenantRepository


class FixedAuth:
    """集成测试只固定身份；授权仍在真实 PostgreSQL RLS 内执行。"""

    def __init__(self, actor_id: UUID) -> None:
        self._actor_id = actor_id

    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        assert access_token == "integration-token"
        return AuthenticatedIdentity(self._actor_id, "integration@example.test")


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
def test_supply_api_uses_real_rls_and_constrained_commands(product_database_url: str) -> None:
    asyncio.run(_verify_supply_api(product_database_url))


async def _verify_supply_api(database_url: str) -> None:
    organization_id, other_id, actor_id = uuid4(), uuid4(), uuid4()
    await _insert_fixture(database_url, organization_id, other_id, actor_id)
    try:
        api = TestClient(
            create_app(
                FixedAuth(actor_id),
                PostgresTenantRepository(database_url),
                PostgresSupplyRepository(),
            )
        )
        headers = {"authorization": "Bearer integration-token"}
        org = str(organization_id)
        for path, payload in [
            (
                "/v1/supply/facts/inventory",
                {
                    "organizationId": org,
                    "partNumber": "PART-A",
                    "locationCode": "MAIN",
                    "onHandQty": "10",
                    "reservedQty": "2",
                    "inTransitQty": "0",
                    "verifiedAt": "2026-10-12T00:00:00Z",
                },
            ),
            (
                "/v1/supply/facts/demands",
                {
                    "organizationId": org,
                    "sourceType": "manual",
                    "sourceReference": "test-order",
                    "lineKey": "1",
                    "partNumber": "PART-A",
                    "requiredQty": "12",
                    "requiredOn": "2026-10-13",
                },
            ),
            (
                "/v1/supply/facts/receipts",
                {
                    "organizationId": org,
                    "receiptReference": "test-receipt",
                    "lineKey": "1",
                    "partNumber": "PART-A",
                    "expectedOn": "2026-10-14",
                    "quantity": "4",
                    "status": "in_transit",
                },
            ),
        ]:
            response = api.put(path, headers=headers, json=payload)
            assert response.status_code == 200, response.json()
        response = api.get(
            "/v1/supply/impact",
            headers=headers,
            params={"organization_id": org, "part_number": "PART-A", "as_of": "2026-10-12"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["firstShortageOn"] == "2026-10-13"
        denied = api.get(
            "/v1/supply/facts",
            headers=headers,
            params={"organization_id": str(other_id), "part_number": "PART-A"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "organization_forbidden"
    finally:
        await _delete_fixture(database_url, organization_id, other_id)


async def _insert_fixture(
    database_url: str, organization_id: UUID, other_id: UUID, actor_id: UUID
) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "insert into app.organization (id, slug, display_name) values (%s, %s, %s)",
                [
                    (organization_id, f"f39-{organization_id.hex[:12]}", "F39"),
                    (other_id, f"other-{other_id.hex[:12]}", "Other"),
                ],
            )
            await cursor.execute(
                "insert into app.organization_member (organization_id, user_id, role) values (%s, %s, 'buyer')",
                (organization_id, actor_id),
            )
        await connection.commit()


async def _delete_fixture(database_url: str, organization_id: UUID, other_id: UUID) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "delete from app.supply_receipt where organization_id = any(%s)",
                ([organization_id, other_id],),
            )
            await cursor.execute(
                "delete from app.supply_demand where organization_id = any(%s)",
                ([organization_id, other_id],),
            )
            await cursor.execute(
                "delete from app.inventory_position where organization_id = any(%s)",
                ([organization_id, other_id],),
            )
            await cursor.execute(
                "delete from app.supply_case where organization_id = any(%s)",
                ([organization_id, other_id],),
            )
            await cursor.execute(
                "delete from app.organization where id = any(%s)", ([organization_id, other_id],)
            )
        await connection.commit()
