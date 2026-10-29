"""F51 SQL fences and result/ledger atomicity in disposable databases only."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from product_api.harness import ToolDefinition
from product_api.runtime import PostgresRuntimeStore, WorkerIdentity
from product_api.tenant import PostgresTenantRepository
from product_api.tool_registry import TOOL_REGISTRY
from product_api.tool_runtime import CallIdentity, ToolAbort, ToolRuntime, accept_tool_result

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def tool_database(*migrations, once_migrations=()):
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL not configured")
    name = "f51_test_" + uuid4().hex
    async with await AsyncConnection.connect(
        make_conninfo(source, dbname="postgres"), autocommit=True
    ) as admin:
        await admin.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(conninfo_to_dict(source)["dbname"])
            )
        )
        url = make_conninfo(source, dbname=name)
        try:
            async with await AsyncConnection.connect(url) as c:
                for filename in once_migrations:
                    await c.execute((ROOT / "supabase/migrations" / filename).read_text())
                for filename in ("0030_f51_tool_execution.sql", "0042_runtime_attempt_guard.sql", *migrations):
                    await c.execute((ROOT / "supabase/migrations" / filename).read_text())
                    await c.execute((ROOT / "supabase/migrations" / filename).read_text())
            yield url
        finally:
            await admin.execute(
                sql.SQL("drop database {} with (force)").format(sql.Identifier(name))
            )


async def seed_tool_job(url):
    org, user, worker, case = [uuid4() for _ in range(4)]
    async with await AsyncConnection.connect(url) as c:
        await c.execute(
            "insert into app.organization(id,slug,display_name) values(%s,%s,'F51')", (org, org.hex)
        )
        await c.execute(
            "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
            (org, user),
        )
        await c.execute(
            "insert into app.organization_member(organization_id,user_id,role,scope) values(%s,%s,'maintainer','{\"runtimeWorker\":true}')",
            (org, worker),
        )
        await c.execute(
            "insert into app.supply_case(id,organization_id,title,status,severity,scope) values(%s,%s,'F51','open','high','{\"partNumbers\":[\"PART-A\"]}')",
            (case, org),
        )
    tenants = PostgresTenantRepository(url)
    author = PostgresRuntimeStore(tenants, WorkerIdentity(org, user, "author"))
    await author.begin_supply(case, "PART-A", uuid4().hex)
    store = PostgresRuntimeStore(tenants, WorkerIdentity(org, worker, uuid4().hex))
    job = await store.claim_next(60)
    return org, user, worker, case, store, job


@pytest.mark.integration
def test_tool_ledger_fences_rls_and_transactional_result_acceptance():
    async def scenario():
        async with tool_database() as url:
            org, user, worker, case, store, job = await seed_tool_job(url)
            execution = store.execution_store(job)
            binding = TOOL_REGISTRY.bind(("supply.impact",))
            runtime = ToolRuntime(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda a: store.supply_impact(job, a),
                        execution_store=execution,
                    ),
                ),
                binding,
            )
            inv = uuid4()

            async def prepare(name):
                return await runtime.prepare(
                    CallIdentity(inv, name, batch_id=len(execution_ids) + 1),
                    "supply.impact",
                    {"partNumber": "PART-A"},
                )

            execution_ids = []
            p = await prepare("ok")
            execution_ids.append(p.ticket["execution_id"])
            outcome = await runtime.execute(p)
            assert outcome.result_ref["sourceKind"] == "trace_artifact"
            with pytest.raises(ToolAbort, match="duplicate_call"):
                await runtime.prepare(p.identity, "supply.impact", {"partNumber": "PART-A"})

            async def rows(c):
                return await (
                    await c.execute("select * from app.tool_execution where job_id=%s", (job.id,))
                ).fetchall()

            records = await store._tenants.in_organization(user, org, rows)
            assert (
                len(records) == 1
                and records[0]["result_hash"] == outcome.result_ref["fragmentHash"]
            )
            peer = uuid4()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
                    (org, peer),
                )
            assert not await store._tenants.in_organization(peer, org, rows)
            for change, undo, code in (
                (
                    "update app.organization_member set role='maintainer' where user_id=%s",
                    "update app.organization_member set role='organization_admin' where user_id=%s",
                    "permission_revoked",
                ),
                (
                    'update app.supply_case set scope=\'{"partNumbers":["OTHER"]}\' where id=%s',
                    'update app.supply_case set scope=\'{"partNumbers":["PART-A"]}\' where id=%s',
                    "binding_invalid",
                ),
            ):
                p = await prepare(code)
                execution_ids.append(p.ticket["execution_id"])
                param = user if code == "permission_revoked" else case
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(change, (param,))
                with pytest.raises(ToolAbort, match=code):
                    await runtime.execute(p)
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(undo, (param,))

            # No Artifact is committed when the result reference is invalid.
            async def forged(_):
                async def write(c):
                    artifact_id = await store._traces.append_artifact(
                        c, job.trace_run_id, case, uuid4(), "tool_result", "forgery", {}
                    )
                    return await accept_tool_result(
                        c,
                        {
                            "citation": {
                                **outcome.result_ref,
                                "sourceId": str(artifact_id),
                                "fragmentHash": "0" * 64,
                            }
                        },
                    )

                return await store._tenants.in_organization(worker, org, write)

            bad = ToolRuntime(
                (ToolDefinition("supply.impact", forged, execution_store=execution),), binding
            )
            p = await bad.prepare(
                CallIdentity(inv, "forged", batch_id=4), "supply.impact", {"partNumber": "PART-A"}
            )
            with pytest.raises(ToolAbort, match="persistence_failed"):
                await bad.execute(p)
            async with await AsyncConnection.connect(url) as c:
                assert (
                    await (
                        await c.execute(
                            "select count(*) from app.trace_artifact where run_id=%s and redacted_summary='forgery'",
                            (job.trace_run_id,),
                        )
                    ).fetchone()
                )[0] == 0
            p = await runtime.prepare(
                CallIdentity(inv, "expired", batch_id=5), "supply.impact", {"partNumber": "PART-A"}
            )
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.runtime_job set lease_expires_at=clock_timestamp()-interval '1 second' where id=%s",
                    (job.id,),
                )
            with pytest.raises(ToolAbort, match="lease_lost"):
                await runtime.execute(p)

            async def reap(c):
                return (await (await c.execute("select app.tool_reap() as n")).fetchone())["n"]

            assert await store._tenants.in_organization(worker, org, reap) >= 3

    asyncio.run(scenario())


@pytest.mark.integration
def test_source_withdrawn_after_acceptance_is_not_delivered():
    from product_api.knowledge import (
        DocumentKind,
        DocumentVisibility,
        KnowledgeChunkInput,
        PostgresKnowledgeRepository,
        SourceKind,
    )

    async def scenario():
        async with tool_database() as url:
            org, user, _, _, store, job = await seed_tool_job(url)
            knowledge = PostgresKnowledgeRepository()

            async def publish(c):
                doc = await knowledge.create_document(
                    c,
                    "Fixture policy",
                    DocumentKind.BUSINESS_POLICY,
                    DocumentVisibility.ORGANIZATION,
                    SourceKind.SYNTHETIC,
                    None,
                )
                version = await knowledge.publish_version(
                    c,
                    doc,
                    "v1",
                    "synthetic:f51",
                    "approval fixture",
                    (KnowledgeChunkInput(0, "section:approval", "approval fixture"),),
                )
                return version

            version = await store._tenants.in_organization(user, org, publish)
            execution = store.execution_store(job)
            r = ToolRuntime(
                (
                    ToolDefinition(
                        "knowledge.search",
                        lambda a: store.knowledge_search(job, a),
                        execution_store=execution,
                    ),
                ),
                TOOL_REGISTRY.bind(("knowledge.search",)),
            )
            p = await r.prepare(
                CallIdentity(uuid4(), "read"), "knowledge.search", {"query": "approval"}
            )
            outcome = await r.execute(p)
            assert outcome.payload["hits"]
            await execution.check_delivery([outcome])
            await store._tenants.in_organization(
                user, org, lambda c: knowledge.revoke_version(c, version, "fixture withdrawn")
            )
            with pytest.raises(ToolAbort, match="permission_revoked"):
                await execution.check_delivery([outcome])

    asyncio.run(scenario())
