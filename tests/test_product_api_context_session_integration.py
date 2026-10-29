"""C2 durable preparation/one-use fencing, using disposable databases only."""

import asyncio
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.context_session import ContextSession
from product_api.context_store import ContextActor, ContextError, ConversationStore
from product_api.harness import HarnessContext, ToolDefinition, ToolRegistry


@pytest.mark.integration
def test_context_session_prepares_each_call_consumes_once_and_commits_final_tail():
    async def scenario():
        async with tool_database(
            "0032_f52_context_sources.sql", "0033_f52_context_snapshots.sql"
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            session = ContextSession(runtime._tenants, runtime._identity, job)
            invocation = uuid4()
            tools = ToolRegistry(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda a: runtime.supply_impact(job, a),
                        execution_store=runtime.execution_store(job),
                    ),
                )
            )
            context = HarnessContext(
                org, job.run_id, job.case_id, "到货未知，不要承诺", role="supply_risk"
            )
            await session.start(context, invocation, tools)
            messages = [{"role": "user", "content": context.task}]
            identifier, projection, ready = await session.prepare(
                {"requestId": "first", "modelCallKey": f"{invocation}-1"}, {"messages": messages}
            )
            assert ready["snapshotId"] == str(identifier)
            assert projection["memoryStatus"] == "disabled"
            assert session.watermark == 1

            async def consume(c):
                await c.execute(
                    "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                    (
                        identifier,
                        job.trace_run_id,
                        ready["modelCallKey"],
                        ready["policyHash"],
                        ready["projectionHash"],
                        "a" * 64,
                    ),
                )

            await session.store._transaction(consume)
            with pytest.raises(ContextError, match="context_snapshot_invalid"):
                await session.store._transaction(consume)
            _, _, second = await session.prepare(
                {"requestId": "second", "modelCallKey": f"{invocation}-2"}, {"messages": messages}
            )
            assert second["snapshotId"] != ready["snapshotId"]
            # A source change invalidates an otherwise correctly bound ready.
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.supply_case set title='changed source' where id=%s", (job.case_id,)
                )

            async def stale(c):
                await c.execute(
                    "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                    (
                        second["snapshotId"],
                        job.trace_run_id,
                        second["modelCallKey"],
                        second["policyHash"],
                        second["projectionHash"],
                        "b" * 64,
                    ),
                )

            with pytest.raises(ContextError, match="context_snapshot_invalid"):
                await session.store._transaction(stale)
            final = [
                *messages,
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "到货仍未知"}],
                    "stopReason": "stop",
                },
            ]
            await session.finish({"messages": final})
            owner = ConversationStore(runtime._tenants, ContextActor(org, user))
            stored = await owner.read_range(session.conversation_id)
            assert [m["role"] for m in stored] == ["user", "assistant"]
            assert stored[-1]["payload"]["content"][0]["text"] == "到货仍未知"
            rebuilt = await session.store.rebuild(session.conversation_id, job)
            assert rebuilt["state"].goal == context.task
            assert rebuilt["conversation"]["state"] == "closed"
            other_role = ContextSession(
                runtime._tenants, runtime._identity, job, conversation_id=session.conversation_id
            )
            with pytest.raises(ContextError, match="context_scope_changed"):
                await other_role.start(
                    HarnessContext(org, job.run_id, job.case_id, context.task, role="customer_ops"),
                    uuid4(),
                    tools,
                )

    asyncio.run(scenario())


@pytest.mark.integration
def test_context_snapshot_rejects_mutation_cross_call_and_old_generation():
    async def scenario():
        async with tool_database(
            "0032_f52_context_sources.sql", "0033_f52_context_snapshots.sql"
        ) as url:
            org, _, _, _, runtime, job = await seed_tool_job(url)
            session = ContextSession(runtime._tenants, runtime._identity, job)
            inv = uuid4()
            tools = ToolRegistry(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda a: runtime.supply_impact(job, a),
                        execution_store=runtime.execution_store(job),
                    ),
                )
            )
            await session.start(
                HarnessContext(org, job.run_id, job.case_id, "Investigate"), inv, tools
            )
            identifier, _, ready = await session.prepare(
                {"requestId": "r", "modelCallKey": f"{inv}-1"},
                {"messages": [{"role": "user", "content": "Investigate"}]},
            )
            for key, policy, projection in [
                (f"{uuid4()}-1", ready["policyHash"], ready["projectionHash"]),
                (ready["modelCallKey"], "changed-policy", ready["projectionHash"]),
                (ready["modelCallKey"], ready["policyHash"], "0" * 64),
            ]:

                async def invalid(c, key=key, policy=policy, projection=projection):
                    await c.execute(
                        "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                        (identifier, job.trace_run_id, key, policy, projection, "c" * 64),
                    )

                with pytest.raises(ContextError, match="context_snapshot_invalid"):
                    await session.store._transaction(invalid)
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.runtime_job set attempts=attempts+1 where id=%s", (job.id,)
                )

            async def old(c):
                await c.execute(
                    "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                    (
                        identifier,
                        job.trace_run_id,
                        ready["modelCallKey"],
                        ready["policyHash"],
                        ready["projectionHash"],
                        "d" * 64,
                    ),
                )

            with pytest.raises(ContextError, match="lease_lost"):
                await session.store._transaction(old)

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["openai", "zhipu"])
def test_context_v3_real_pi_business_roundtrips(monkeypatch, provider):
    import os

    from dotenv import load_dotenv
    from test_product_api_responses_integration import scenario

    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL not configured")
    asyncio.run(scenario(source, monkeypatch, provider, context_enabled=True))


@pytest.mark.integration
@pytest.mark.parametrize("cancel_during_prepare", [False, True])
def test_context_v3_replay_commit_and_cancel_preparation(cancel_during_prepare):
    from pathlib import Path

    from product_api.harness import Harness, MemoryTraceSink, PiRuntimeAdapter

    async def scenario():
        async with tool_database(
            "0032_f52_context_sources.sql", "0033_f52_context_snapshots.sql"
        ) as url:
            org, _, _, _, runtime, job = await seed_tool_job(url)
            session = ContextSession(runtime._tenants, runtime._identity, job)
            context = HarnessContext(
                org, job.run_id, job.case_id, "Investigate", invocation_id=uuid4()
            )
            tools = ToolRegistry(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda a: runtime.supply_impact(job, a),
                        execution_store=runtime.execution_store(job),
                    ),
                )
            )
            cancel = asyncio.Event()
            entered = asyncio.Event()
            if cancel_during_prepare:

                async def prepare(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                session.compiler.prepare = prepare
            adapter = PiRuntimeAdapter(
                (
                    "node",
                    str(
                        Path(__file__).resolve().parents[1] / "pi_sidecar/f42_protocol_sidecar.mjs"
                    ),
                ),
                timeout_seconds=10,
                protocol_version=3,
                context_session=session,
            )
            task = asyncio.create_task(
                Harness(adapter, tools).execute(context, MemoryTraceSink(), cancel)
            )
            if cancel_during_prepare:
                await asyncio.wait_for(entered.wait(), 5)
                cancel.set()
            result = await asyncio.wait_for(task, 5)
            assert result.status == ("cancelled" if cancel_during_prepare else "completed")
            async with await AsyncConnection.connect(url) as c:
                snapshots = await (
                    await c.execute(
                        "select state,request_hash from app.context_snapshot where invocation_id=%s",
                        (context.invocation_id,),
                    )
                ).fetchall()
                conversation = await (
                    await c.execute(
                        "select state,committed_watermark from app.context_conversation where id=%s",
                        (session.conversation_id,),
                    )
                ).fetchone()
                if cancel_during_prepare:
                    assert snapshots == [] and conversation == ("open", 1)
                else:
                    assert snapshots == [("accepted", None)] * 3
                    assert conversation == ("closed", 6)

    asyncio.run(scenario())


@pytest.mark.integration
def test_context_gateway_rejects_changed_body_schema_missing_ready_and_reuse():
    import copy
    import time
    from dataclasses import replace

    import httpx

    from product_api.gateway import (
        CapabilitySigner,
        ModelCapability,
        ModelGateway,
        PostgresModelAccounting,
        ReplayResponsesProvider,
        create_model_gateway_app,
    )

    class Provider(ReplayResponsesProvider):
        def __init__(self):
            self.requests = []

        async def stream(self, policy, payload):
            self.requests.append(payload)
            async for chunk in super().stream(policy, payload):
                yield chunk

    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
        ) as url:
            org, _, _, _, runtime, original_job = await seed_tool_job(url)
            policy_id = uuid4()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "insert into app.model_policy(id,organization_id,provider,model_id,real_calls_enabled,daily_budget_micros,max_call_cost_micros,max_output_tokens,context_window) values(%s,%s,'openai','fixture',true,1000,100,128,32768)",
                    (policy_id, org),
                )
            job = replace(original_job, model_policy_id=policy_id)
            session = ContextSession(runtime._tenants, runtime._identity, job)
            inv = uuid4()
            tools = ToolRegistry(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda a: runtime.supply_impact(job, a),
                        execution_store=runtime.execution_store(job),
                    ),
                )
            )
            await session.start(
                HarnessContext(org, job.run_id, job.case_id, "Investigate"), inv, tools
            )
            signer = CapabilitySigner(b"synthetic-context-gateway-signing-key")
            scope = ModelCapability(
                runtime._identity.actor_id, org, job.trace_run_id, policy_id, int(time.time()) + 60
            )
            provider = Provider()
            app = create_model_gateway_app(
                ModelGateway(
                    signer, PostgresModelAccounting(runtime._tenants), {"openai": provider}
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for ordinal, damage in enumerate(("text", "schema", "headers", "hash", "valid"), 1):
                    _, projection, ready = await session.prepare(
                        {"requestId": f"r{ordinal}", "modelCallKey": f"{inv}-{ordinal}"},
                        {"messages": [{"role": "user", "content": "Investigate"}]},
                    )
                    payload = {
                        "model": "fixture",
                        "stream": True,
                        "input": [
                            {"role": "system", "content": projection["systemPrompt"]},
                            {
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Investigate"}],
                            },
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "name": t["wireName"],
                                "description": t["description"],
                                "parameters": t["inputSchema"],
                            }
                            for t in projection["tools"]
                        ],
                    }
                    headers = {
                        "authorization": f"Bearer {signer.issue(scope)}",
                        "x-supplyagent-call-key": ready["modelCallKey"],
                        "x-supplyagent-policy-hash": ready["policyHash"],
                        "x-supplyagent-context-snapshot": ready["snapshotId"],
                        "x-supplyagent-projection-hash": ready["projectionHash"],
                    }
                    changed = copy.deepcopy(payload)
                    if damage == "text":
                        changed["input"][1]["content"][0]["text"] = "Injected instruction"
                    elif damage == "schema":
                        changed["tools"][0]["parameters"] = {"type": "object"}
                    elif damage == "headers":
                        headers.pop("x-supplyagent-context-snapshot")
                        headers.pop("x-supplyagent-projection-hash")
                    elif damage == "hash":
                        headers["x-supplyagent-projection-hash"] = "0" * 64
                    response = await client.post("/v1/responses", json=changed, headers=headers)
                    assert response.status_code == (200 if damage == "valid" else 403)
                    assert len(provider.requests) == (1 if damage == "valid" else 0)
                    if damage == "valid":
                        repeated = await client.post("/v1/responses", json=changed, headers=headers)
                        assert repeated.status_code == 403 and len(provider.requests) == 1
                async with await AsyncConnection.connect(url) as c:
                    count = await (
                        await c.execute("select count(*) from app.model_exchange")
                    ).fetchone()
                    assert count[0] == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_context_captures_rejected_non_object_arguments_without_forging_success():
    import json

    from product_api.tool_runtime import CallIdentity

    async def scenario():
        async with tool_database(
            "0032_f52_context_sources.sql", "0033_f52_context_snapshots.sql"
        ) as url:
            org, _, _, _, runtime, job = await seed_tool_job(url)
            session = ContextSession(runtime._tenants, runtime._identity, job)
            inv = uuid4()
            tools = ToolRegistry(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda a: runtime.supply_impact(job, a),
                        execution_store=runtime.execution_store(job),
                    ),
                )
            )
            await session.start(
                HarnessContext(org, job.run_id, job.case_id, "Investigate"), inv, tools
            )
            identity = CallIdentity(inv, "rejected|original", 1, 0, None)
            outcome = await tools.runtime.prepare(identity, "supply.impact", None)
            assert outcome.status == "error" and outcome.code == "invalid_arguments"
            session.remember_batch([(identity, "supply.impact", None)], [outcome])
            messages = [
                {"role": "user", "content": "Investigate"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": identity.call_id,
                            "name": "supply.impact",
                            "arguments": None,
                        }
                    ],
                },
                {
                    "role": "toolResult",
                    "toolCallId": identity.call_id,
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps(outcome.payload)}],
                },
            ]
            await session.capture(messages)
            stored = await session.store.read_range(session.conversation_id)
            assert stored[-1]["payload"] == {
                "status": "error",
                "code": "invalid_arguments",
                "resultRef": None,
            }
            assert stored[-2]["payload"]["toolCalls"][0]["arguments"] is None
            identifier, _, ready = await session.prepare(
                {"requestId": "rejected", "modelCallKey": f"{inv}-1"}, {"messages": messages}
            )
            await session.abandon()

            async def cancelled(c):
                await c.execute(
                    "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                    (
                        identifier,
                        job.trace_run_id,
                        ready["modelCallKey"],
                        ready["policyHash"],
                        ready["projectionHash"],
                        None,
                    ),
                )

            with pytest.raises(ContextError, match="context_snapshot_invalid"):
                await session.store._transaction(cancelled)
            messages[-1]["content"][0]["text"] = "{}"
            with pytest.raises(ContextError, match="context_history_changed"):
                await session.capture(messages)

    asyncio.run(scenario())
