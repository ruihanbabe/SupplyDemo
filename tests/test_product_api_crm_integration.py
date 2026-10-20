"""F36 P3 reads F32 CRM facts only through an RLS tenant transaction."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection

from product_api.crm import CrmFactAccessError, PostgresCrmFactRepository
from product_api.tenant import PostgresTenantRepository, TenantAccessError


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
def test_crm_facts_are_scoped_and_cross_organization_reads_fail(product_database_url: str) -> None:
    asyncio.run(_verify(product_database_url))


async def _verify(database_url: str) -> None:
    organization_id, other_organization_id, actor_id, hidden_actor_id = uuid4(), uuid4(), uuid4(), uuid4()
    opportunity_id, scoped_opportunity_id, hidden_opportunity_id = await _insert_fixture(
        database_url, organization_id, other_organization_id, actor_id, hidden_actor_id
    )
    try:
        tenants = PostgresTenantRepository(database_url)
        facts = PostgresCrmFactRepository()
        visible = await tenants.in_organization(
            actor_id, organization_id, lambda connection: facts.load_opportunity_facts(connection, opportunity_id)
        )
        assert visible.customer.display_name == "Aurora Spectrum Lab"
        assert visible.opportunity.stage == "negotiation"
        assert [item.summary for item in visible.conversations] == ["Customer requested notice before substitution."]
        assert [item.title for item in visible.tasks] == ["Verify SKY13453 delivery impact"]
        assert all(reference.citation().source_kind == "crm_record" for reference in visible.references())

        with pytest.raises(CrmFactAccessError):
            await tenants.in_organization(
                actor_id, organization_id,
                lambda connection: facts.load_opportunity_facts(connection, scoped_opportunity_id),
            )
        with pytest.raises(TenantAccessError):
            await tenants.in_organization(
                actor_id, other_organization_id,
                lambda connection: facts.load_opportunity_facts(connection, hidden_opportunity_id),
            )
        with pytest.raises(CrmFactAccessError):
            await tenants.in_organization(
                actor_id, organization_id,
                lambda connection: facts.load_opportunity_facts(connection, hidden_opportunity_id),
            )
    finally:
        await _delete_fixture(database_url, organization_id, other_organization_id)


async def _insert_fixture(
    database_url: str, organization_id: UUID, other_organization_id: UUID, actor_id: UUID, hidden_actor_id: UUID
) -> tuple[UUID, UUID, UUID]:
    opportunity_id, scoped_opportunity_id, hidden_opportunity_id = uuid4(), uuid4(), uuid4()
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "insert into app.organization (id, slug, display_name) values (%s, %s, %s)",
                [(organization_id, f"crm-facts-{organization_id.hex[:10]}", "CRM Facts"),
                 (other_organization_id, f"crm-other-{other_organization_id.hex[:10]}", "Other")],
            )
            await cursor.executemany(
                "insert into app.organization_member (organization_id, user_id, role) values (%s, %s, 'sales_manager')",
                [(organization_id, actor_id), (organization_id, hidden_actor_id),
                 (other_organization_id, hidden_actor_id)],
            )
            await cursor.execute(
                """insert into app.crm_customer (organization_id, display_name, lifecycle, created_by)
                   values (%s, 'Aurora Spectrum Lab', 'active', %s) returning id""",
                (organization_id, actor_id),
            )
            customer_id = (await cursor.fetchone())[0]
            await cursor.execute(
                """insert into app.crm_opportunity (id, organization_id, customer_id, title, stage, created_by)
                   values (%s, %s, %s, 'December RF modules', 'negotiation', %s)""",
                (opportunity_id, organization_id, customer_id, actor_id),
            )
            await cursor.execute(
                """insert into app.crm_contact (organization_id, customer_id, display_name, email, created_by)
                   values (%s, %s, 'Ming Li', 'ming@example.test', %s)""",
                (organization_id, customer_id, actor_id),
            )
            await cursor.execute(
                """insert into app.crm_conversation (organization_id, customer_id, opportunity_id, channel, summary, created_by)
                   values (%s, %s, %s, 'email', 'Customer requested notice before substitution.', %s)""",
                (organization_id, customer_id, opportunity_id, actor_id),
            )
            await cursor.execute(
                """insert into app.crm_task (organization_id, opportunity_id, title, created_by)
                   values (%s, %s, 'Verify SKY13453 delivery impact', %s)""",
                (organization_id, opportunity_id, actor_id),
            )
            await cursor.execute(
                """insert into app.crm_customer (organization_id, display_name, scope, created_by)
                   values (%s, 'Scoped Customer', %s::jsonb, %s) returning id""",
                (organization_id, '{"member_ids":[]}', hidden_actor_id),
            )
            scoped_customer_id = (await cursor.fetchone())[0]
            await cursor.execute(
                """insert into app.crm_opportunity (id, organization_id, customer_id, title, stage, scope, created_by)
                   values (%s, %s, %s, 'Scoped opportunity', 'lead', %s::jsonb, %s)""",
                (scoped_opportunity_id, organization_id, scoped_customer_id, '{"member_ids":[]}', hidden_actor_id),
            )
            await cursor.execute(
                """insert into app.crm_customer (organization_id, display_name, scope, created_by)
                   values (%s, 'Hidden Customer', %s::jsonb, %s) returning id""",
                (other_organization_id, '{"member_ids":[]}', hidden_actor_id),
            )
            hidden_customer_id = (await cursor.fetchone())[0]
            await cursor.execute(
                """insert into app.crm_opportunity (id, organization_id, customer_id, title, stage, scope, created_by)
                   values (%s, %s, %s, 'Hidden opportunity', 'lead', %s::jsonb, %s)""",
                (hidden_opportunity_id, other_organization_id, hidden_customer_id, '{"member_ids":[]}', hidden_actor_id),
            )
        await connection.commit()
    return opportunity_id, scoped_opportunity_id, hidden_opportunity_id


async def _delete_fixture(database_url: str, organization_id: UUID, other_organization_id: UUID) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("delete from app.crm_task where organization_id = any(%s)", ([organization_id, other_organization_id],))
            await cursor.execute("delete from app.crm_conversation where organization_id = any(%s)", ([organization_id, other_organization_id],))
            await cursor.execute("delete from app.crm_contact where organization_id = any(%s)", ([organization_id, other_organization_id],))
            await cursor.execute("delete from app.crm_proposal where organization_id = any(%s)", ([organization_id, other_organization_id],))
            await cursor.execute("delete from app.crm_opportunity where organization_id = any(%s)", ([organization_id, other_organization_id],))
            await cursor.execute("delete from app.crm_customer where organization_id = any(%s)", ([organization_id, other_organization_id],))
            await cursor.execute("delete from app.organization where id = any(%s)", ([organization_id, other_organization_id],))
        await connection.commit()
