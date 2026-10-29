"""F49/F50 HTTP -> Worker -> real Pi -> OpenAI-compatible fixtures -> PostgreSQL/RLS."""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import uvicorn
from dotenv import load_dotenv
from f51_batch_business_fixture import BatchBusinessFixture
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_product_api_business_nodes_integration import seed_story, story_id
from test_product_api_commercial_integration import seed_commercial
from test_product_api_glm import NativeGlmFixture

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.gateway import (
    CapabilitySigner,
    ModelGateway,
    PostgresModelAccounting,
    ReplayResponsesProvider,
    create_model_gateway_app,
)
from product_api.tenant import PostgresTenantRepository
from product_api.trace import PostgresTraceRepository
from product_api.worker_main import create_worker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["openai", "zhipu"])
def test_responses_business_flow_and_private_model_exchange(monkeypatch, provider):
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local PostgreSQL is not configured")
    asyncio.run(scenario(source, monkeypatch, provider))


@pytest.mark.integration
def test_oversized_batch_preserves_evidence_without_a_second_model_request(monkeypatch):
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local PostgreSQL is not configured")
    asyncio.run(scenario(source, monkeypatch, "openai", overflow=True))


async def scenario(source, monkeypatch, provider, overflow=False, context_enabled=False):
    name = "f49_scenario_" + uuid4().hex
    async with await AsyncConnection.connect(
        make_conninfo(source, dbname="postgres"), autocommit=True
    ) as admin:
        await admin.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(conninfo_to_dict(source)["dbname"])
            )
        )
        try:
            await in_clone(
                make_conninfo(source, dbname=name), monkeypatch, provider, overflow, context_enabled
            )
        finally:
            await admin.execute(
                sql.SQL("drop database {} with (force)").format(sql.Identifier(name))
            )


async def in_clone(url, monkeypatch, provider, overflow=False, context_enabled=False):
    policy_id = uuid4()
    model_id = "glm-4.7-flash" if provider == "zhipu" else "SYNTHETIC-Responses-fixture"
    async with await AsyncConnection.connect(url) as db:
        for filename in (
            "0024_f45_intelligence.sql",
            "0025_f46_business_nodes.sql",
            "0026_f47_bom_quotes.sql",
            "0027_f49_responses.sql",
            "0028_f50_glm.sql",
            "0028_f50_glm.sql",
            "0030_f51_tool_execution.sql",
            "0042_runtime_attempt_guard.sql",
            "0031_f51_tool_resources.sql",
        ):
            await db.execute((ROOT / "supabase/migrations" / filename).read_text())
        if context_enabled:
            for filename in ("0032_f52_context_sources.sql", "0033_f52_context_snapshots.sql"):
                await db.execute((ROOT / "supabase/migrations" / filename).read_text())
        clock = await seed_story(db)
        await seed_commercial(db, clock)
        # Explicit test-only ingestion; this policy reaches an in-process fixture, never OpenAI.
        await db.execute(
            "insert into app.model_policy(id,organization_id,provider,model_id,real_calls_enabled,daily_budget_micros,max_call_cost_micros,max_output_tokens,context_window) values (%s,%s,%s,%s,true,1000,100,4096,32768)",
            (policy_id, story_id("org"), provider, model_id),
        )
        await db.commit()
    if context_enabled:
        from product_api.context_runtime import ContextPolicy

        settings = ContextPolicy.load().settings
        settings["enabled"] = True
        fixed_policy = ContextPolicy.from_dict(settings)
        monkeypatch.setattr(ContextPolicy, "load", classmethod(lambda cls, path=None: fixed_policy))
    signing_key = "synthetic-responses-signing-key-32-bytes"
    tenants = PostgresTenantRepository(url)
    gateway = create_model_gateway_app(
        ModelGateway(
            CapabilitySigner(signing_key.encode()),
            PostgresModelAccounting(tenants),
            {
                provider: BatchBusinessFixture(
                    NativeGlmFixture() if provider == "zhipu" else ReplayResponsesProvider(),
                    provider == "zhipu",
                    repeat_all=overflow,
                )
            },
        )
    )
    server = uvicorn.Server(uvicorn.Config(gateway, log_level="error", lifespan="off"))
    listening = socket.socket()
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    port = listening.getsockname()[1]
    running = asyncio.create_task(server.serve(sockets=[listening]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        for key, value in {
            "SUPPLYAGENT_PRODUCT_DATABASE_URL": url,
            "SUPPLYAGENT_GATEWAY_URL": f"http://127.0.0.1:{port}/v1",
            "SUPPLYAGENT_GATEWAY_SIGNING_KEY": signing_key,
            "SUPPLYAGENT_WORKER_ORGANIZATION_ID": str(story_id("org")),
            "SUPPLYAGENT_WORKER_ACTOR_ID": str(story_id("worker")),
            "SUPPLYAGENT_WORKER_OWNER": "f49-worker",
            "SUPPLYAGENT_WORKER_REPLAY": "0",
            "SUPPLYAGENT_COMMERCIAL_NODES_ENABLED": "1",
            "SUPPLYAGENT_RESPONSES_ENABLED": "1",
            "SUPPLYAGENT_MODEL_GATEWAY_ENABLED": "1",
        }.items():
            monkeypatch.setenv(key, value)

        class Auth:
            async def verify_access_token(self, token):
                return AuthenticatedIdentity(story_id(token), "synthetic@example.invalid")

        app = create_app(auth_verifier=Auth(), tenant_repository=tenants, trace_repository=PostgresTraceRepository())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"authorization": "Bearer sales"},
        ) as client:
            response = await client.post(
                "/v1/intelligence/runs",
                json={
                    "organization_id": str(story_id("org")),
                    "opportunity_id": str(story_id("opportunity/chengchuan")),
                    "question": "比较 BOM 需求和报价有效期",
                    "idempotency_key": "f49-business",
                    "mode": "gateway",
                    "model_policy_id": str(policy_id),
                    "as_of": clock.isoformat(),
                },
            )
            assert response.status_code == 202, response.text
            run = response.json()["data"]
            worker_report = await create_worker().run_once_report()
            async with await AsyncConnection.connect(url) as diagnostics:
                failure_codes = await (
                    await diagnostics.execute(
                        "select metadata->>'code' from app.trace_event where event_type='pi.turn_failed' and run_id=%s",
                        (worker_report.trace_run_id,),
                    )
                ).fetchall()
                ledger_states = await (
                    await diagnostics.execute(
                        "select canonical_name,state,error_code from app.tool_execution where mission_id=%s",
                        (run["missionId"],),
                    )
                ).fetchall()
            if overflow:
                assert (
                    worker_report.outcome == "retry_scheduled"
                    and ("protocol_failed",) in failure_codes
                )
                async with await AsyncConnection.connect(url) as diagnostics:
                    failed = await (
                        await diagnostics.execute(
                            "select metadata->>'invocationId' from app.trace_event where event_type='pi.turn_failed' and metadata->>'code'='protocol_failed' and run_id=%s",
                            (worker_report.trace_run_id,),
                        )
                    ).fetchall()
                    assert failed
                    for (invocation,) in failed:
                        records = await (
                            await diagnostics.execute(
                                "select state,result_id,result_hash from app.tool_execution where invocation_id=%s",
                                (invocation,),
                            )
                        ).fetchall()
                        assert len(records) == 3 and all(
                            r[0] == "succeeded" and r[1] and r[2] for r in records
                        )
                        assert (
                            await (
                                await diagnostics.execute(
                                    "select count(*) from app.model_exchange where invocation_id=%s",
                                    (invocation,),
                                )
                            ).fetchone()
                        )[0] == 1
                return
            assert worker_report.outcome == "completed", (
                worker_report.outcome,
                worker_report.error_code,
                failure_codes,
                ledger_states,
            )
            result = await client.get(
                "/v1/intelligence/runs/" + run["runId"],
                params={"organization_id": str(story_id("org"))},
            )
            answer = result.json()["data"]
            assert answer["status"] == "completed", answer
            assert answer["mode"] == "gateway" and answer["result"] is not None
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select e.invocation_id,e.call_ordinal,e.request,e.response,e.state,a.role from app.model_exchange e join app.intelligence_attempt a on a.id=e.invocation_id where a.mission_id=%s order by a.role,e.call_ordinal",
                (run["missionId"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) == 10, [(r[1], r[-1], r[-2]) for r in rows]
            assert all(r[4] == "completed" for r in rows)
            assert all(
                (
                    r[2].get("store") is False
                    if provider == "openai"
                    else r[2]["thinking"] == {"type": "disabled"}
                )
                for r in rows
            )
            assert all(r[2]["model"] == model_id for r in rows)
            identities = {(str(r[0]), r[1]) for r in rows}
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s",
                (run["missionId"],),
            )
            observations = [r[0] for r in await cursor.fetchall()]
            assert len(observations) == 7
            ledger = await (
                await db.execute(
                    "select e.batch_id,e.call_index,e.state,e.result_hash,o.content_hash from app.tool_execution e join app.intelligence_observation o on o.id=e.result_id where e.mission_id=%s",
                    (run["missionId"],),
                )
            ).fetchall()
            assert len(ledger) == 7
            assert all(r[0] == 1 and r[2] == "succeeded" and r[3] == r[4] for r in ledger)
            assert sorted(r[1] for r in ledger) == sorted([0] * 5 + [1, 2])
            for observation in observations:
                execution = observation["execution"]
                invocation, ordinal = execution["modelCallKey"].rsplit("-", 1)
                assert invocation == execution["invocationId"]
                assert (invocation, int(ordinal)) in identities
                assert (
                    ("|fc_" in execution["toolCallId"])
                    if provider == "openai"
                    else (execution["toolCallId"].startswith("call_glm_fixture"))
                )
            cursor = await db.execute(
                "select metadata from app.trace_event where run_id=(select trace_run_id from app.runtime_run where id=%s)",
                (UUID(run["runId"]),),
            )
            metadata = str(await cursor.fetchall())
            assert "modelCallKey" in metadata
            assert "比较 BOM" not in metadata and "SYNTHETIC-supplier" not in metadata

        async def count_private(c):
            if context_enabled:
                counts = await (
                    await c.execute(
                        "select count(*) as total,count(context_snapshot_id) as snapshots,count(projection_hash) as projections,count(request_hash) as requests from app.model_exchange"
                    )
                ).fetchone()
                assert len(set(counts.values())) == 1
                invalid = await (
                    await c.execute(
                        "select count(*) as n from app.model_exchange e join app.context_snapshot s on s.id=e.context_snapshot_id where s.state<>'accepted' or e.projection_hash<>s.projection_hash or e.request_hash<>s.request_hash or e.invocation_id<>s.invocation_id"
                    )
                ).fetchone()
                assert invalid["n"] == 0
                open_conversations = await (
                    await c.execute(
                        "select count(*) as n from app.context_conversation where state<>'closed'"
                    )
                ).fetchone()
                assert open_conversations["n"] == 0
            cursor = await c.execute("select count(*) as n from app.model_exchange")
            return (await cursor.fetchone())["n"]

        assert (
            await tenants.in_organization(story_id("worker"), story_id("org"), count_private) == 10
        )
        assert await tenants.in_organization(story_id("sales"), story_id("org"), count_private) == 0
        assert (
            await tenants.in_organization(
                story_id("foreign-user"), story_id("foreign-org"), count_private
            )
            == 0
        )
        from product_api.evaluation_result import assess, snapshot

        captured = await snapshot(tenants, story_id("sales"), story_id("org"), UUID(run["runId"]))
        score = assess(captured, {
            "caseId": "bom-quote-" + provider, "version": 1,
            "organizationId": str(story_id("org")), "question": "比较 BOM 需求和报价有效期",
            "expected": {"quality": "needs_review", "commercialNodesEnabled": True},
        })
        assert score["hardConstraints"] == "passed", score
        assert score["semanticQuality"] == "pending"
        assert score["usageState"] == "settled" and score["costMicros"] == 0
        assert len(score["attempts"]) == 5
        assert len(score["evidenceLinks"]) == 7
        assert all(link["modelCallKey"] for link in score["evidenceLinks"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as reader:
            response = await reader.get(
                f"/v1/intelligence/runs/{run['runId']}/evaluation",
                params={"organization_id": str(story_id("org"))},
                headers={"authorization": "Bearer sales"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["runId"] == run["runId"]
            denied = await reader.get(
                f"/v1/intelligence/runs/{run['runId']}/evaluation",
                params={"organization_id": str(story_id("foreign-org"))},
                headers={"authorization": "Bearer foreign-user"},
            )
            assert denied.status_code == 403
            timeline = await reader.get(
                f"/v1/intelligence/runs/{run['runId']}/events",
                params={"organization_id": str(story_id("org"))},
                headers={"authorization": "Bearer sales"},
            )
            assert timeline.status_code == 200 and "event: complete" in timeline.text
            event_ids = [line[4:] for line in timeline.text.splitlines() if line.startswith("id: ")]
            assert event_ids and len(event_ids) == len(set(event_ids))
            assert "比较 BOM" not in timeline.text and "SYNTHETIC-supplier" not in timeline.text
            resumed = await reader.get(
                f"/v1/intelligence/runs/{run['runId']}/events",
                params={"organization_id": str(story_id("org"))},
                headers={"authorization": "Bearer sales", "last-event-id": event_ids[-1]},
            )
            assert resumed.status_code == 200 and "event: trace" not in resumed.text
        from product_api.tenant import TenantAccessError

        with pytest.raises(TenantAccessError):
            await snapshot(tenants, story_id("foreign-user"), story_id("foreign-org"), UUID(run["runId"]))
    finally:
        server.should_exit = True
        await asyncio.wait_for(running, 5)
