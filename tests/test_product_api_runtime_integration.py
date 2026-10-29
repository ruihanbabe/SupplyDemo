"""F35 persisted Worker turn in an isolated clone of the local test database."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
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
from product_api.runtime import (
    DurableWorker,
    PostgresRuntimeStore,
    RuntimeJob,
    WorkerIdentity,
    WorkerPolicy,
)
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
    async with await AsyncConnection.connect(database_url) as migration_db:
        migration = (Path(__file__).resolve().parents[1] / "supabase/migrations/0030_f51_tool_execution.sql").read_text()
        await migration_db.execute(migration)
        await migration_db.execute(migration)
        await migration_db.execute((Path(__file__).resolve().parents[1] / "supabase/migrations/0031_f51_tool_resources.sql").read_text())
        await migration_db.execute((Path(__file__).resolve().parents[1] / "supabase/migrations/0042_runtime_attempt_guard.sql").read_text())
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
    rejected = await worker.run_once_report()
    assert rejected.outcome == "claim_rejected" and rejected.job_id == legacy_job_id
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
            assert (await cursor.fetchone())["n"] == 11
            await cursor.execute(
                "select event_type,metadata from app.trace_event where run_id=%s order by sequence_no",
                (trace_run_id,),
            )
            events = await cursor.fetchall()
            assert len([e for e in events if e["event_type"] != "runtime.checkpoint"]) == 8
            assert [e["metadata"]["phase"] for e in events if e["event_type"] == "runtime.checkpoint"] == [
                "claimed", "business_completed", "before_terminal",
            ]
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


    await _lifecycle_in_clone(database_url, organization_id, actor_id, worker_id, case_id)


async def _lifecycle_in_clone(url, org, actor, worker_id, case_id):
    tenants = PostgresTenantRepository(url)
    author = PostgresRuntimeStore(tenants, WorkerIdentity(org, actor, "author"))
    left = PostgresRuntimeStore(tenants, WorkerIdentity(org, worker_id, "left"))
    right = PostgresRuntimeStore(tenants, WorkerIdentity(org, worker_id, "right"))
    await author.begin_supply(case_id, "PART-A", uuid4().hex)
    claims = await asyncio.gather(left.claim_next(30), right.claim_next(30))
    assert sum(isinstance(item, RuntimeJob) for item in claims) == 1
    index = next(i for i, item in enumerate(claims) if isinstance(item, RuntimeJob))
    store = (left, right)[index]
    job = claims[index]
    assert await store.fail_outcome(job, "model_failed", WorkerPolicy(30, 5, 3, 0)) == "queued"
    again = await store.claim_next(30)
    assert again.generation == job.generation + 1
    assert not await store.complete(job)
    assert await store.fail_outcome(again, "protocol_failed", WorkerPolicy(30, 5, 1, 0)) == "failed"
    assert await store.claim_next(30) is None

    fixture = """import json,sys
start=json.loads(sys.stdin.readline())
print(json.dumps({"protocolVersion":1,"type":"turn.started","turnId":start["turnId"]}),flush=True)
command=json.loads(sys.stdin.readline())
assert command["type"]=="turn.cancel"
print(json.dumps({"protocolVersion":1,"type":"turn.cancelled","turnId":start["turnId"]}),flush=True)
"""
    program = """import asyncio,sys
from product_api import worker_main as wm
from product_api.harness import PiRuntimeAdapter
original=wm.create_worker
def create():
    worker=original()
    worker._engine_factory=lambda job: PiRuntimeAdapter((sys.executable,"-c",sys.argv[1]),timeout_seconds=30)
    return worker
wm.create_worker=create
raise SystemExit(asyncio.run(wm.run_worker(once=True,poll_seconds=2)))
"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _, _, job_id = await author.begin_supply(case_id, "PART-A", uuid4().hex)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", program, fixture,
            env={
                "PATH": os.environ["PATH"], "PYTHONPATH": "src",
                "SUPPLYAGENT_PRODUCT_DATABASE_URL": url,
                "SUPPLYAGENT_WORKER_ORGANIZATION_ID": str(org),
                "SUPPLYAGENT_WORKER_ACTOR_ID": str(worker_id),
                "SUPPLYAGENT_WORKER_OWNER": "lifecycle-test",
                "SUPPLYAGENT_WORKER_LEASE_SECONDS": "2",
                "SUPPLYAGENT_WORKER_HEARTBEAT_SECONDS": ".2",
                "SUPPLYAGENT_WORKER_REPLAY": "1",
            },
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        try:
            async with asyncio.timeout(10):
                async with await AsyncConnection.connect(url) as db:
                    while True:
                        row = await (await db.execute(
                            "select j.status,j.attempts,exists(select 1 from app.trace_event e where e.run_id=r.trace_run_id and e.event_type='pi.turn_started') from app.runtime_job j join app.runtime_run r on r.id=j.run_id where j.id=%s",
                            (job_id,),
                        )).fetchone()
                        if row[2]:
                            break
                        assert process.returncode is None
                        await db.commit()
                        await asyncio.sleep(.05)
                if sig == signal.SIGKILL:
                    os.killpg(process.pid, sig)
                else:
                    process.send_signal(sig)
                stdout, stderr = await process.communicate()
                if sig == signal.SIGTERM:
                    assert process.returncode == 0 and not stderr
                    assert json.loads(stdout)["outcome"] == "cancelled"
                    with pytest.raises(ProcessLookupError):
                        os.killpg(process.pid, 0)
                else:
                    assert process.returncode == -signal.SIGKILL
                    await asyncio.sleep(2.1)
                    assert await left.reap() == 1
                    recovered = await left.claim_next(30)
                    assert recovered.id == job_id and recovered.generation == 2
                    assert await left.fail_outcome(
                        recovered, "protocol_failed", WorkerPolicy(30, 5, 1, 0),
                    ) == "failed"
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.communicate()
