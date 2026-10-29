"""C3 coverage/epoch/read permissions in an automatically removed clone."""

import asyncio
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from test_product_api_context_store_integration import recorded_tool_unit
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.context_compaction import ContextReader
from product_api.context_runtime import ContextPolicy
from product_api.context_session import ContextSession
from product_api.context_store import ContextActor, ContextError, ConversationStore
from product_api.harness import HarnessContext, ToolDefinition, ToolRegistry


@pytest.mark.integration
def test_compaction_coverage_epoch_and_authorized_progressive_read():
    async def scenario():
        async with tool_database(
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            settings = ContextPolicy.load().settings
            settings["inputCap"] = 1800
            session = ContextSession(
                runtime._tenants, runtime._identity, job, ContextPolicy.from_dict(settings)
            )
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
            text = "2026-10-27：未确认到货，不应作出承诺；原因是样品未通过。"
            messages = [
                {"role": "user", "content": "Investigate"},
                *[
                    {"role": "assistant", "content": [{"type": "text", "text": text}]}
                    for _ in range(30)
                ],
                {"role": "assistant", "content": [{"type": "text", "text": "latest unknown"}]},
            ]
            # Schema occupies the actual window too; leave enough space for protected input.
            _, projection, _ = await session.prepare(
                {"requestId": "r", "modelCallKey": f"{inv}-1"}, {"messages": messages}
            )
            assert projection["summaryStatus"] == "compacted"
            owner = ConversationStore(runtime._tenants, ContextActor(org, user))
            reader = ContextReader(owner, session.conversation_id)
            summary = await reader.read("L1")
            assert text in summary["value"]["compaction"]["text"]
            assert len(summary["value"]["compaction"]["sourceManifest"]) == 30
            assert (await reader.read("L2", after=1))["value"]["messages"][0]["payload"]["content"][
                0
            ]["text"] == text
            pair, outcome = await recorded_tool_unit(runtime, job)
            await session.store.commit_turn(
                session.conversation_id, session.watermark, "read-source", (pair,), job=job
            )
            evidence = await reader.read("L0", evidence=outcome.result_ref)
            assert evidence["value"]["bytes"] > 0
            page = await reader.read("L2", evidence=outcome.result_ref)
            assert page["value"]["fields"]
            peer = uuid4()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
                    (org, peer),
                )
            with pytest.raises(ContextError, match="context_denied"):
                await ContextReader(
                    ConversationStore(runtime._tenants, ContextActor(org, peer)),
                    session.conversation_id,
                ).read("L1")
            # Even a cache/read-by-id must not serve a summary after dependencies change.
            with pytest.raises(ContextError, match="context_summary_invalid"):
                await reader.read("L1")
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.organization_member set role='maintainer' where user_id=%s and organization_id=%s",
                    (user, org),
                )
            with pytest.raises(ContextError, match="context_denied"):
                await reader.read("L0", evidence=outcome.result_ref)

    asyncio.run(scenario())
