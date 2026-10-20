"""F35 persisted Worker turn in an isolated clone of the local test database."""

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
from product_api.runtime import DurableWorker, PostgresRuntimeStore, WorkerIdentity, WorkerPolicy
from product_api.tenant import PostgresTenantRepository
from product_api.trace import PostgresTraceRepository

SIDECAR = Path(__file__).resolve().parents[1] / "pi_sidecar" / "f42_protocol_sidecar.mjs"


@pytest.mark.integration
def test_worker_commits_trace_and_job_with_real_pi_and_rls() -> None:
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL URL not configured")
    asyncio.run(_scenario(source))


async def _scenario(source: str) -> None:
    database_name = f"f35_test_{uuid4().hex}"
    parts = conninfo_to_dict(source)
    source_name = parts["dbname"]
    admin_url = make_conninfo(source, dbname="postgres")
    cloned_url = make_conninfo(source, dbname=database_name)
    async with await AsyncConnection.connect(admin_url, autocommit=True) as admin:
        await admin.execute(sql.SQL("create database {} template {}").format(
            sql.Identifier(database_name), sql.Identifier(source_name),
        ))
        try:
            await _run_in_clone(cloned_url)
        finally:
            await admin.execute(sql.SQL("drop database {} with (force)").format(
                sql.Identifier(database_name),
            ))


async def _run_in_clone(database_url: str) -> None:
    organization_id, actor_id, worker_id = uuid4(), uuid4(), uuid4()
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "insert into app.organization(id,slug,display_name) values (%s,%s,'F35 test')",
                (organization_id, f"f35-{organization_id.hex}"),
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
                "insert into app.supply_case(organization_id,title,status,severity,scope) values (%s,'F35','open','high','{\"partNumbers\":[\"PART-A\"]}') returning id",
                (organization_id,),
            )
            case_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.runtime_mission(organization_id,kind,created_by) values (%s,'legacy',%s) returning id",
                (organization_id, actor_id),
            )
            legacy_mission_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.runtime_run(organization_id,mission_id) values (%s,%s) returning id",
                (organization_id, legacy_mission_id),
            )
            legacy_run_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.runtime_job(organization_id,run_id,idempotency_key,kind) values (%s,%s,'legacy-no-trace','agent_turn') returning id",
                (organization_id, legacy_run_id),
            )
            legacy_job_id = (await cursor.fetchone())[0]
        await connection.commit()

    tenants = PostgresTenantRepository(database_url)
    class FixedAuth:
        async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
            assert access_token == "fixture-token"
            return AuthenticatedIdentity(actor_id, "fixture@example.test")

    app = create_app(FixedAuth(), tenants)
    payload = {
        "organizationId": str(organization_id), "caseId": str(case_id),
        "partNumber": "PART-A", "idempotencyKey": "f35-integration",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        first = await client.post(
            "/v1/runtime/supply-runs", json=payload,
            headers={"authorization": "Bearer fixture-token"},
        )
        assert first.status_code == 200
        repeated = await client.post(
            "/v1/runtime/supply-runs", json=payload,
            headers={"authorization": "Bearer fixture-token"},
        )
        assert repeated.status_code == 200 and repeated.json() == first.json()
    job_id = UUID(first.json()["data"]["jobId"])
    identity = WorkerIdentity(organization_id, worker_id, "f35-worker-1")
    store = PostgresRuntimeStore(tenants, identity)
    worker = DurableWorker(
        store, identity, WorkerPolicy(30, 5, 2, 0),
        lambda _: PiRuntimeAdapter(("node", str(SIDECAR)), timeout_seconds=10),
    )
    assert not await worker.run_once()
    assert await worker.run_once()

    async def inspect(connection):
        async with connection.cursor() as cursor:
            await cursor.execute(
                "select j.status, r.trace_run_id from app.runtime_job j join app.runtime_run r on r.id=j.run_id where j.id=%s",
                (job_id,),
            )
            row = await cursor.fetchone()
            assert row["status"] == "completed"
            trace_run_id = row["trace_run_id"]
            await cursor.execute("select count(*) as n from app.trace_event where run_id=%s", (trace_run_id,))
            assert (await cursor.fetchone())["n"] == 8
            await cursor.execute(
                """select id, artifact_type, version, redacted_payload, content_hash
                   from app.trace_artifact where run_id=%s
                     and artifact_type in ('tool_result','specialist_result')
                   order by created_at, version""",
                (trace_run_id,),
            )
            artifacts = await cursor.fetchall()
            tool_results = [item for item in artifacts if item["artifact_type"] == "tool_result"]
            specialist = [item for item in artifacts if item["artifact_type"] == "specialist_result"]
            assert len(tool_results) == 2 and len(specialist) == 1
            citation = specialist[0]["redacted_payload"]["claims"][0]["citations"][0]
            assert citation == {
                "sourceId": str(tool_results[-1]["id"]),
                "sourceKind": "trace_artifact",
                "version": str(tool_results[-1]["version"]),
                "locator": "redacted_payload",
                "fragmentHash": tool_results[-1]["content_hash"],
            }
            await cursor.execute("select status from app.runtime_job where id=%s", (legacy_job_id,))
            assert (await cursor.fetchone())["status"] == "failed"
            return trace_run_id

    trace_run_id = await tenants.in_organization(worker_id, organization_id, inspect)
    assert await tenants.in_organization(
        worker_id, organization_id,
        lambda connection: PostgresTraceRepository().verify_chain(connection, trace_run_id),
    )
