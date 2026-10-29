"""M1 SQL authority + actual Mem0, using disposable PostgreSQL and synthetic inputs."""

import asyncio
from uuid import UUID

import pytest
from test_product_api_mem0_backend_integration import embeddings_fixture
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.context_runtime import ContextCompiler
from product_api.context_store import ContextActor, ConversationStore, MessageUnit
from product_api.mem0_adapter import Mem0Adapter, MemoryBackendError
from product_api.memory_runtime import MemoryService, MemoryWorker
from product_api.memory_store import MemoryStore


@pytest.mark.integration
def test_process_summary_capture_publish_recall_and_stable_confirmation(tmp_path):
    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            actor = ContextActor(org, user)
            owner = MemoryStore(runtime._tenants, actor)
            conversations = ConversationStore(runtime._tenants, actor)
            conversation = await conversations.open(job.id, "supply_risk")
            await owner.enable(conversation, synthetic=True)
            text = "Prior decision: arrival remains unconfirmed because samples did not pass; do not promise delivery."
            await conversations.commit_turn(conversation, 0, "one", (MessageUnit.user(text),))
            await conversations.close(conversation, 1)
            with embeddings_fixture() as (endpoint, usage):

                def backend(scope):
                    return Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)

                worker_store = MemoryStore(runtime._tenants, runtime._identity)
                worker = MemoryWorker(worker_store, backend)
                report = await worker.run_once()
                assert report["status"] == "indexed", report

                async def load(c):
                    return await (
                        await c.execute(
                            "select * from app.memory_source where conversation_id=%s",
                            (conversation,),
                        )
                    ).fetchone()

                source = await owner.transaction(load)
                scope = owner.scope(source)
                service = MemoryService(owner, backend)
                recalled = await service.recall("What was the prior decision?", scope=scope)
                assert recalled["status"] == "available" and len(recalled["items"]) == 1
                item = recalled["items"][0]
                assert item["trust"] == "generated_reference" and text in item["body"]
                assert item["sources"][0]["conversationId"] == str(conversation)
                assert await service.read(UUID(item["versionId"]), scope=scope, level="L2")
                assert (await worker.run_once())["status"] == "idle"
                assert (await service.recall("Current stock?", scope=scope))[
                    "status"
                ] == "not_requested"
                await owner.remember(
                    scope.object_scope,
                    "Prefer to review unresolved risks before agreeing on delivery.",
                )
                pending = await worker.run_once()
                assert pending["status"] == "review_pending"
                assert len((await service.recall("Prior preference?", scope=scope))["items"]) == 1
                await owner.confirm(UUID(pending["versionId"]))
                assert (await worker.run_once())["status"] == "indexed"
                items = (await service.recall("Prior preference?", scope=scope))["items"]
                assert {i["trust"] for i in items} == {"generated_reference", "confirmed"}
                projection = await ContextCompiler(memory=service).prepare(
                    [{"role": "user", "content": "What was the prior decision?"}],
                    "trusted rules",
                    [],
                    {"contextWindow": 32768, "maxTokens": 128},
                    scope=scope,
                )
                assert len(projection["memoryItems"]) == 2
                assert projection["systemPrompt"] == "trusted rules"
                assert (
                    text in projection["messages"][-2]["content"]
                    or text in projection["messages"][-1]["content"]
                )
                corrected = await service.correct(
                    UUID(pending["versionId"]),
                    "Prefer written risk review before discussing delivery.",
                )
                assert corrected
                assert len((await service.recall("Prior preference?", scope=scope))["items"]) == 1
                corrected_report = await worker.run_once()
                assert corrected_report["status"] == "review_pending"
                await owner.confirm(UUID(corrected_report["versionId"]))
                assert (await worker.run_once())["status"] == "indexed"
                removed = await service.forget(
                    UUID(item["versionId"]), mode="hard", include_source=True
                )
                assert (
                    removed["state"] == "backup_pending" and removed["inventory"]["sql"] == "erased"
                )
                remaining = (await service.recall("Prior preference?", scope=scope))["items"]
                assert len(remaining) == 1 and remaining[0]["trust"] == "confirmed"
                with pytest.raises(MemoryBackendError, match="memory_denied"):
                    await service.read(UUID(item["versionId"]), scope=scope)
                with pytest.raises(MemoryBackendError, match="memory_denied"):
                    await service.correct(UUID(item["versionId"]), "Revive erased history.")
                source_page = await conversations.read_range(conversation)
                assert source_page[0]["payload"] == {"erased": True}

                async def private(c):
                    return await (
                        await c.execute(
                            "select body,status from app.memory_version where id=%s",
                            (UUID(item["versionId"]),),
                        )
                    ).fetchone()

                tombstone = await owner.transaction(private)
                assert tombstone["body"] is None and tombstone["status"] == "deleted"
                assert usage

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("accept_old_first", [False, True])
def test_delayed_prefix_cannot_replace_newer_indexed_summary(tmp_path, accept_old_first):
    from psycopg import AsyncConnection

    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = MemoryStore(runtime._tenants, ContextActor(org, user))
            conversations = ConversationStore(runtime._tenants, owner.actor)
            conversation = await conversations.open(job.id, "delayed_summary")
            await owner.enable(conversation, synthetic=True)
            await conversations.commit_turn(
                conversation, 0, "first", (MessageUnit.user("Prior delivery was approved."),)
            )
            await conversations.commit_turn(
                conversation,
                1,
                "correction",
                (MessageUnit.user("Correction: delivery is NOT approved; samples failed."),),
            )
            await conversations.close(conversation, 2)
            # Reproduce a legacy overlapping prefix alongside the new incremental ranges.
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.memory_source s set start_ordinal=1,source_hash=(select encode(public.digest(convert_to(jsonb_agg(jsonb_build_object('ordinal',ordinal,'hash',content_hash) order by ordinal)::text,'UTF8'),'sha256'),'hex') from app.context_message where conversation_id=s.conversation_id and ordinal<=s.watermark) where conversation_id=%s and watermark=2",
                    (conversation,),
                )
            worker = MemoryStore(runtime._tenants, runtime._identity)
            old, newer = await worker.claim(), await worker.claim()
            old_source, _ = await worker.source(old["source_id"])
            new_source, _ = await worker.source(newer["source_id"])
            assert old_source["watermark"] == 1 and new_source["watermark"] == 2
            old_version = None
            if accept_old_first:
                old_version = await worker.accept(
                    old, old_source["source_epoch"], "Prior delivery was approved."
                )
            new_version = await worker.accept(
                newer, new_source["source_epoch"], "Delivery is NOT approved; samples failed."
            )
            with embeddings_fixture() as (endpoint, usage):

                def backend(scope):
                    return Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)

                await worker.publish(newer, new_source, new_version, backend)
                before = len(usage)
                with pytest.raises(MemoryBackendError, match="memory_source_changed"):
                    if accept_old_first:
                        await worker.publish(old, old_source, old_version, backend)
                    else:
                        await worker.accept(
                            old, old_source["source_epoch"], "Prior delivery was approved."
                        )
                assert len(usage) == before
                service = MemoryService(owner, backend)
                items = (await service.recall("Prior decision?", scope=owner.scope(new_source)))[
                    "items"
                ]
                assert len(items) == 1 and items[0]["versionId"] == str(new_version["id"])
                forgotten = await service.forget(new_version["id"], mode="soft")
                assert forgotten["state"] == "completed"
                with pytest.raises(MemoryBackendError, match="memory_denied"):
                    await service.correct(new_version["id"], "Revive withdrawn history.")
                assert not (await service.recall("Prior decision?", scope=owner.scope(new_source)))[
                    "items"
                ]

    asyncio.run(scenario())


@pytest.mark.integration
def test_lease_fencing_unknown_publication_reconciliation_and_private_scope(tmp_path):
    from uuid import uuid4

    from psycopg import AsyncConnection

    from product_api.mem0_adapter import MemoryScope

    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = MemoryStore(runtime._tenants, ContextActor(org, user))
            conversations = ConversationStore(runtime._tenants, owner.actor)
            conversation = await conversations.open(job.id, "source_recovery")
            await owner.enable(conversation, synthetic=True)
            await conversations.commit_turn(
                conversation, 0, "one", (MessageUnit.user("Prior samples were not approved."),)
            )
            await conversations.close(conversation, 1)
            worker_store = MemoryStore(runtime._tenants, runtime._identity)
            old = await worker_store.claim()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.memory_work_item set lease_until=clock_timestamp()-interval '1 second' where id=%s",
                    (old["id"],),
                )
            current = await worker_store.claim()
            source, _ = await worker_store.source(current["source_id"])
            with pytest.raises(MemoryBackendError, match="memory_lease_lost"):
                await worker_store.accept(
                    old, source["source_epoch"], "Old publication cannot be accepted."
                )
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.memory_work_item set lease_until=clock_timestamp()-interval '1 second' where id=%s",
                    (current["id"],),
                )
            with embeddings_fixture() as (endpoint, usage):

                def backend(scope):
                    return Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)

                class Unknown:
                    def __init__(self, scope):
                        self.backend = backend(scope)

                    def publish(self, *args):
                        self.backend.publish(*args)
                        raise OSError("synthetic interruption after remote success")

                    def bind_meter(self, meter):
                        self.backend.bind_meter(meter)

                    def close(self):
                        self.backend.close()

                report = await MemoryWorker(worker_store, Unknown).run_once()
                assert report == {
                    "status": "reconciliation_required",
                    "code": "memory_write_outcome_unknown",
                }
                count = len(usage)
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.memory_work_item set lease_until=clock_timestamp()-interval '1 second' where id=%s",
                        (current["id"],),
                    )
                assert (await MemoryWorker(worker_store, backend).run_once())["status"] == "indexed"
                assert len(usage) == count
                scope = owner.scope(source)
                service = MemoryService(owner, backend)
                item = (await service.recall("Prior decision?", scope=scope))["items"][0]
                peer, foreign = uuid4(), uuid4()
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
                        (org, peer),
                    )
                    await c.execute(
                        "insert into app.organization(id,slug,display_name) values(%s,%s,'foreign')",
                        (foreign, foreign.hex),
                    )
                    await c.execute(
                        "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
                        (foreign, peer),
                    )
                for organization in (org, foreign):
                    outsider = MemoryStore(runtime._tenants, ContextActor(organization, peer))
                    with pytest.raises(MemoryBackendError, match="memory_denied"):
                        await outsider.forget(UUID(item["versionId"]), "hard", True)
                    outsider_service = MemoryService(outsider, backend)
                    with pytest.raises(MemoryBackendError, match="memory_denied"):
                        await outsider_service.read(
                            UUID(item["versionId"]),
                            scope=MemoryScope(organization, peer, scope.object_scope),
                        )
                # Revocation must block hydration even if the vector backend still contains the item.
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.organization_member set role='maintainer' where organization_id=%s and user_id=%s",
                        (org, user),
                    )
                assert (await service.recall("Prior decision?", scope=scope))["items"] == []

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    "sensitive",
    ["api_key=sk-synthetic-never-send", '{"credentials":{"api_key":"opaque-never-send"}}'],
)
def test_sensitive_source_quarantines_before_any_backend_call(sensitive):
    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = MemoryStore(runtime._tenants, ContextActor(org, user))
            conversations = ConversationStore(runtime._tenants, owner.actor)
            conversation = await conversations.open(job.id, "sensitive")
            await owner.enable(conversation, synthetic=True)
            await conversations.commit_turn(conversation, 0, "one", (MessageUnit.user(sensitive),))
            await conversations.close(conversation, 1)

            def backend(_):
                pytest.fail("Sensitive source reached the backend")

            report = await MemoryWorker(
                MemoryStore(runtime._tenants, runtime._identity), backend
            ).run_once()
            assert report == {"status": "quarantined", "code": "memory_sensitive_content"}

    asyncio.run(scenario())


@pytest.mark.integration
def test_real_pi_captures_sources_and_injects_memory_into_next_invocation(tmp_path):
    from dataclasses import replace
    from pathlib import Path
    from uuid import uuid4

    from psycopg import AsyncConnection

    from product_api.context_session import ContextSession
    from product_api.harness import (
        Harness,
        HarnessContext,
        MemoryTraceSink,
        PiRuntimeAdapter,
        ToolDefinition,
        ToolRegistry,
    )

    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, _, _, _, runtime, job = await seed_tool_job(url)
            with embeddings_fixture() as (endpoint, _usage):

                def backend(scope):
                    return Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)

                async def turn(current_job, task):
                    session = ContextSession(
                        runtime._tenants,
                        runtime._identity,
                        current_job,
                        memory_backend_factory=backend,
                    )
                    tools = ToolRegistry(
                        (
                            ToolDefinition(
                                "supply.impact",
                                lambda args: runtime.supply_impact(current_job, args),
                                execution_store=runtime.execution_store(current_job),
                            ),
                        )
                    )
                    adapter = PiRuntimeAdapter(
                        (
                            "node",
                            str(
                                Path(__file__).resolve().parents[1]
                                / "pi_sidecar/f42_protocol_sidecar.mjs"
                            ),
                        ),
                        protocol_version=3,
                        context_session=session,
                        timeout_seconds=30,
                    )
                    context = HarnessContext(
                        org,
                        current_job.run_id,
                        current_job.case_id,
                        task,
                        invocation_id=uuid4(),
                        role="supply_risk",
                    )
                    result = await Harness(adapter, tools).execute(context, MemoryTraceSink())
                    assert result.status == "completed"
                    return session, context

                first, _ = await turn(job, "Investigate")
                worker = MemoryWorker(MemoryStore(runtime._tenants, runtime._identity), backend)
                for _ in range(10):
                    report = await worker.run_once()
                    if report["status"] == "idle":
                        break
                    assert report["status"] == "indexed", report
                else:
                    pytest.fail("Memory work did not drain")
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.runtime_job set attempts=attempts+1 where id=%s", (job.id,)
                    )
                second, context = await turn(
                    replace(job, generation=job.generation + 1), "What was the prior decision?"
                )
                assert second.conversation_id != first.conversation_id
                async with await AsyncConnection.connect(url) as c:
                    snapshots = await (
                        await c.execute(
                            "select projection from app.context_snapshot where invocation_id=%s order by model_call_key",
                            (context.invocation_id,),
                        )
                    ).fetchall()
                    assert len(snapshots) == 3
                    assert all(s[0]["memoryItems"] for s in snapshots)
                    assert all(
                        item["trust"] == "generated_reference"
                        for s in snapshots
                        for item in s[0]["memoryItems"]
                    )
                    # Injected references are not recaptured as new independent source messages.
                    raw = await (
                        await c.execute(
                            "select payload from app.context_message where conversation_id=%s",
                            (second.conversation_id,),
                        )
                    ).fetchall()
                    assert "Historical memory reference;" not in str(raw)

    asyncio.run(scenario())


@pytest.mark.integration
def test_long_source_incremental_ranges_cover_all_units_and_survive_out_of_order_publish(tmp_path):
    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = MemoryStore(runtime._tenants, ContextActor(org, user))
            conversations = ConversationStore(runtime._tenants, owner.actor)
            conversation = await conversations.open(job.id, "long_history")
            await owner.enable(conversation, synthetic=True)
            topics = [
                "supplier investigation",
                "sales follow-up",
                "customer preference",
                "manager review",
            ]
            units = tuple(
                MessageUnit.user(
                    f"Prior {topics[i % 4]}, step {i}: 2026-10-27, still unconfirmed because samples failed; "
                    "only review after approval. Do not promise delivery. "
                    + "Background remains under review. "
                    * 50
                )
                for i in range(24)
            )
            assert sum(len(u.model_dump_json().encode()) for u in units) > 32768
            assert await conversations.commit_turn(conversation, 0, "large", units) == len(units)
            assert await conversations.commit_turn(conversation, 0, "large", units) == len(units)
            tail = MessageUnit.user(
                "Final correction: NOT approved; next review date remains unknown."
            )
            await conversations.commit_turn(conversation, len(units), "tail", (tail,))
            await conversations.close(conversation, len(units) + 1)

            async def load(c):
                return await (
                    await c.execute(
                        "select * from app.memory_source where conversation_id=%s order by start_ordinal",
                        (conversation,),
                    )
                ).fetchall()

            sources = await owner.transaction(load)
            assert len(sources) > 1
            covered = [i for s in sources for i in range(s["start_ordinal"], s["watermark"] + 1)]
            assert covered == list(range(1, len(units) + 2))
            assert sources[-1]["start_ordinal"] == len(units) + 1
            worker_store = MemoryStore(runtime._tenants, runtime._identity)
            work = [await worker_store.claim() for _ in sources]
            assert await worker_store.claim() is None
            from product_api.memory_runtime import summarize

            with embeddings_fixture() as (endpoint, _usage):

                def backend(scope):
                    return Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)

                service = MemoryService(owner, backend)
                rendered = []
                # Later disjoint ranges must not suppress unprocessed earlier history.
                for item in reversed(work):
                    source, rows = await worker_store.source(item["source_id"])
                    body = summarize(source, rows)
                    version = await worker_store.accept(item, source["source_epoch"], body)
                    await worker_store.publish(item, source, version, backend)
                    value = await service.read(version["id"], scope=owner.scope(source))
                    assert value["coverage"]["start"] == source["start_ordinal"]
                    assert value["coverage"]["end"] == source["watermark"]
                    assert value["coverage"]["sourceHash"] == source["source_hash"]
                    assert len(value["coverage"]["units"]) == len(rows)
                    assert len(value["body"].encode()) <= 8192
                    rendered.append(value["body"])
                for unit in (*units, tail):
                    assert any(unit.messages[0].payload["text"] in body for body in rendered)

                # All disjoint summaries remain readable, not just the last accepted version.
                async def versions(c):
                    return await (
                        await c.execute(
                            "select id,body,status from app.memory_version where assertion_id=%s",
                            (conversation,),
                        )
                    ).fetchall()

                rows = await owner.transaction(versions)
                assert len(
                    await owner.hydrate(owner.scope(sources[0]), [str(r["id"]) for r in rows])
                ) == len(sources)
                removed = await service.forget(rows[0]["id"], mode="hard", include_source=True)
                assert removed["state"] == "backup_pending"
                assert (
                    await owner.hydrate(owner.scope(sources[0]), [str(r["id"]) for r in rows]) == []
                )
                assert all(
                    r["body"] is None and r["status"] == "deleted"
                    for r in await owner.transaction(versions)
                )
                erased_backend = backend(owner.scope(sources[0]))
                try:
                    assert all(erased_backend.reconcile(r["id"]) == [] for r in rows)
                finally:
                    erased_backend.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_oversized_unit_is_quarantined_without_blocking_next_complete_unit(tmp_path):
    async def scenario():
        async with tool_database(
            "0027_f49_responses.sql",
            "0032_f52_context_sources.sql",
            "0033_f52_context_snapshots.sql",
            "0034_f52_context_compaction.sql",
            "0035_f48_memory.sql",
            "0036_f48_memory_ranges.sql",
            "0037_f48_memory_operations.sql",
            "0038_f48_memory_lifecycle.sql",
    "0039_f48_relations.sql",
    "0040_f48_legacy_bridge.sql",
    "0041_f48_dream.sql",
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = MemoryStore(runtime._tenants, ContextActor(org, user))
            conversations = ConversationStore(runtime._tenants, owner.actor)
            conversation = await conversations.open(job.id, "oversized_history")
            await owner.enable(conversation, synthetic=True)
            await conversations.commit_turn(
                conversation,
                0,
                "large-unit",
                (
                    MessageUnit.user("Unknown; do not promise delivery. " * 1200),
                    MessageUnit.user(
                        "Prior final decision: still unconfirmed because samples failed."
                    ),
                ),
            )
            await conversations.close(conversation, 2)
            with embeddings_fixture() as (endpoint, usage):

                def backend(scope):
                    return Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)

                worker = MemoryWorker(MemoryStore(runtime._tenants, runtime._identity), backend)
                report = await worker.run_once()
                assert report == {"status": "quarantined", "code": "memory_content_invalid"}
                assert usage == []
                assert (await worker.run_once())["status"] == "indexed"
                assert (await worker.run_once())["status"] == "idle"
                assert usage

    asyncio.run(scenario())
