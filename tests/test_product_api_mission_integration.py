"""F36 opportunity Mission through HTTP, SQL Worker, real Pi Replay, and F40."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.harness import PiRuntimeAdapter
from product_api.knowledge import (
    DocumentKind,
    DocumentVisibility,
    KnowledgeChunkInput,
    PostgresKnowledgeRepository,
    SourceKind,
)
from product_api.runtime import DurableWorker, PostgresRuntimeStore, WorkerIdentity, WorkerPolicy
from product_api.tenant import PostgresTenantRepository
from product_api.trace import PostgresTraceRepository

SIDECAR = Path(__file__).resolve().parents[1] / "pi_sidecar" / "f42_protocol_sidecar.mjs"


@pytest.mark.integration
def test_opportunity_mission_persists_specialists_reviews_and_parent() -> None:
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL URL not configured")
    asyncio.run(_scenario(source))


async def _scenario(source: str) -> None:
    database_name = f"f36_test_{uuid4().hex}"
    source_name = conninfo_to_dict(source)["dbname"]
    admin_url = make_conninfo(source, dbname="postgres")
    cloned_url = make_conninfo(source, dbname=database_name)
    async with await AsyncConnection.connect(admin_url, autocommit=True) as admin:
        await admin.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(database_name),
                sql.Identifier(source_name),
            )
        )
        try:
            await _run_in_clone(cloned_url)
        finally:
            await admin.execute(
                sql.SQL("drop database {} with (force)").format(
                    sql.Identifier(database_name),
                )
            )


async def _run_in_clone(database_url: str) -> None:
    organization_id, actor_id, worker_id = uuid4(), uuid4(), uuid4()
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "insert into app.organization(id,slug,display_name) values (%s,%s,'F36 demo')",
                (organization_id, f"f36-{organization_id.hex}"),
            )
            await cursor.execute(
                "insert into app.organization_member(organization_id,user_id,role) values (%s,%s,'organization_admin')",
                (organization_id, actor_id),
            )
            await cursor.execute(
                "insert into app.organization_member(organization_id,user_id,role,scope) values (%s,%s,'maintainer','{\"runtimeWorker\":true}')",
                (organization_id, worker_id),
            )
            await cursor.execute(
                "insert into app.supply_case(organization_id,title,status,severity,scope) values (%s,'Synthetic SKY13453','open','high','{\"partNumbers\":[\"PART-A\"]}') returning id",
                (organization_id,),
            )
            case_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.crm_customer(organization_id,display_name,lifecycle,created_by) values (%s,'SYNTHETIC Aurora','active',%s) returning id",
                (organization_id, actor_id),
            )
            customer_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.crm_opportunity(organization_id,customer_id,title,stage,supply_case_id,created_by) values (%s,%s,'SYNTHETIC AUR-2026-017','negotiation',%s,%s) returning id",
                (organization_id, customer_id, case_id, actor_id),
            )
            opportunity_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.crm_conversation(organization_id,customer_id,opportunity_id,channel,summary,created_by) values (%s,%s,%s,'email','SYNTHETIC delivery question',%s)",
                (organization_id, customer_id, opportunity_id, actor_id),
            )
            await cursor.execute(
                "insert into app.crm_task(organization_id,opportunity_id,title,created_by) values (%s,%s,'SYNTHETIC verify supply impact',%s)",
                (organization_id, opportunity_id, actor_id),
            )
        await connection.commit()

    tenants = PostgresTenantRepository(database_url)
    knowledge = PostgresKnowledgeRepository()

    async def publish_policy(connection):
        document_id = await knowledge.create_document(
            connection,
            "SYNTHETIC delivery policy",
            DocumentKind.BUSINESS_POLICY,
            DocumentVisibility.ORGANIZATION,
            SourceKind.SYNTHETIC,
            None,
        )
        await knowledge.publish_version(
            connection,
            document_id,
            "demo-v1",
            "synthetic:NRF-COMM-1.0",
            "Approval is required before a customer-facing delivery update.",
            (
                KnowledgeChunkInput(
                    0,
                    "section:approval",
                    "Approval is required before a customer-facing delivery update.",
                ),
            ),
        )

    await tenants.in_organization(actor_id, organization_id, publish_policy)

    class FixedAuth:
        async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
            assert access_token == "fixture-token"
            return AuthenticatedIdentity(actor_id, "fixture@example.test")

    app = create_app(FixedAuth(), tenants)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        response = await client.post(
            "/v1/runtime/opportunity-runs",
            json={
                "organizationId": str(organization_id),
                "opportunityId": str(opportunity_id),
                "idempotencyKey": "f36-mission-integration",
            },
            headers={"authorization": "Bearer fixture-token"},
        )
        assert response.status_code == 200, response.text
    job_id = UUID(response.json()["data"]["jobId"])
    identity = WorkerIdentity(organization_id, worker_id, "f36-worker-1")
    worker = DurableWorker(
        PostgresRuntimeStore(tenants, identity),
        identity,
        WorkerPolicy(60, 5, 2, 0),
        lambda _: PiRuntimeAdapter(("node", str(SIDECAR)), timeout_seconds=15),
    )
    assert await worker.run_once()

    async def inspect(connection):
        async with connection.cursor() as cursor:
            await cursor.execute(
                "select j.status,r.trace_run_id from app.runtime_job j join app.runtime_run r on r.id=j.run_id where j.id=%s",
                (job_id,),
            )
            row = await cursor.fetchone()
            assert row["status"] == "completed"
            trace_run_id = row["trace_run_id"]
            await cursor.execute(
                "select artifact_type,redacted_payload from app.trace_artifact where run_id=%s",
                (trace_run_id,),
            )
            artifacts = await cursor.fetchall()
            types = [item["artifact_type"] for item in artifacts]
            assert types.count("specialist_result") == 4
            assert types.count("specialist_review") == 4
            assert types.count("parent_recommendation") == 1
            parent = next(
                item["redacted_payload"]
                for item in artifacts
                if item["artifact_type"] == "parent_recommendation"
            )
            assert parent["quality"] == "complete"
            assert parent["proposalDraft"]["executable"] is False
            return trace_run_id

    trace_run_id = await tenants.in_organization(worker_id, organization_id, inspect)
    assert await tenants.in_organization(
        worker_id,
        organization_id,
        lambda connection: PostgresTraceRepository().verify_chain(connection, trace_run_id),
    )
