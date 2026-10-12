"""F31 Python TenantRepository 的本地 PostgreSQL 集成验收。"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection

from product_api.tenant import PostgresTenantRepository, TenantAccessError


@pytest.fixture
def product_database_url() -> str:
    """从未提交的本地环境读取 PostgreSQL URL；缺失时显式跳过。"""
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
def test_tenant_repository_uses_restricted_role_and_one_transaction(
    product_database_url: str,
) -> None:
    """成员重验、RLS 上下文与读取必须在同一受限事务内发生。"""
    asyncio.run(_verify_tenant_repository(product_database_url))


async def _verify_tenant_repository(database_url: str) -> None:
    """写入随机 fixture，验证后无条件删除，避免污染本地数据库。"""
    organization_id = uuid4()
    other_organization_id = uuid4()
    actor_id = uuid4()
    await _insert_fixture(database_url, organization_id, other_organization_id, actor_id)
    try:
        repository = PostgresTenantRepository(database_url)
        selected = await repository.select_organization(actor_id, organization_id)
        assert selected.organization_id == organization_id

        async def read_context(connection: AsyncConnection[dict[str, object]]) -> dict[str, object]:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    select current_role as role, app.current_actor_id() as actor_id,
                           app.current_organization_id() as organization_id
                    """
                )
                row = await cursor.fetchone()
            assert row is not None
            return row

        context = await repository.in_organization(actor_id, organization_id, read_context)
        assert context == {
            "role": "supplyagent_api",
            "actor_id": actor_id,
            "organization_id": organization_id,
        }
        with pytest.raises(TenantAccessError):
            await repository.select_organization(actor_id, other_organization_id)
    finally:
        await _delete_fixture(database_url, organization_id, other_organization_id)


async def _insert_fixture(
    database_url: str, organization_id: UUID, other_organization_id: UUID, actor_id: UUID
) -> None:
    """只用管理连接写入隔离验证所需的两组织数据。"""
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "insert into app.organization (id, slug, display_name) values (%s, %s, %s)",
                [
                    (organization_id, f"api-{organization_id.hex[:12]}", "API verification"),
                    (other_organization_id, f"api-{other_organization_id.hex[:12]}", "Other verification"),
                ],
            )
            await cursor.execute(
                """
                insert into app.organization_member (organization_id, user_id, role)
                values (%s, %s, 'organization_admin')
                """,
                (organization_id, actor_id),
            )
        await connection.commit()


async def _delete_fixture(database_url: str, organization_id: UUID, other_organization_id: UUID) -> None:
    """无论断言成功或失败都删除随机组织及级联成员关系。"""
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "delete from app.organization where id = any(%s)",
                ([organization_id, other_organization_id],),
            )
        await connection.commit()
