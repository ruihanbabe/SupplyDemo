"""F48 M1 lifecycle gates using real isolated stores and fixed local model reports."""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_product_api_mem0_backend_integration import embeddings_fixture
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.context_store import ContextActor, ContextError, ConversationStore, MessageUnit
from product_api.mem0_adapter import Mem0Adapter, MemoryBackendError
from product_api.memory_models import LocalModelPort
from product_api.memory_runtime import MemoryService, MemoryWorker, summarize
from product_api.memory_store import MemoryStore
from product_api.tenant import PostgresTenantRepository

MIGRATIONS = (
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
)


@asynccontextmanager
async def memory_fixture(
    text="Prior decision: 2026-10-27, NOT approved because samples failed; only review after approval.",
):
    async with tool_database(*MIGRATIONS) as url:
        org, user, _, _, runtime, job = await seed_tool_job(url)
        owner = MemoryStore(runtime._tenants, ContextActor(org, user))
        worker = MemoryStore(runtime._tenants, runtime._identity)
        conversations = ConversationStore(runtime._tenants, owner.actor)
        conversation = await conversations.open(job.id, "history")
        await owner.enable(conversation, synthetic=True)
        await conversations.commit_turn(conversation, 0, "one", (MessageUnit.user(text),))
        await conversations.close(conversation, 1)
        yield url, owner, worker, conversations, conversation, runtime, job


async def rows(store, table):
    async def operation(c):
        return await (
            await c.execute(sql.SQL("select * from app.{}").format(sql.Identifier(table)))
        ).fetchall()

    return await store.transaction(operation)


@pytest.mark.integration
@pytest.mark.parametrize(
    "topic", ["supplier investigation", "sales follow-up", "customer preference", "manager review"]
)
def test_common_model_policy_quality_and_persistent_accounting(tmp_path, topic):
    async def scenario():
        text = f"Prior {topic}: 2026-10-27, unconfirmed because samples failed; only review after approval. Do not promise delivery."
        async with memory_fixture(text) as (_, owner, worker, _, _, _, _):
            with embeddings_fixture(summary=lambda p: list(range(len(p)))) as (endpoint, calls):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                result = await MemoryWorker(worker, backend, summary_endpoint=endpoint).run_once()
                assert result["status"] == "indexed", result
                version = (await rows(owner, "memory_version"))[0]
                assert text in version["body"] and version["trust"] == "generated_reference"
                assert version["summary_manifest"]["modelPolicy"] == "grounded-process-v1"
                source = (await rows(owner, "memory_source"))[0]
                assert (
                    await MemoryService(owner, backend).recall(
                        "Prior decision?", scope=owner.scope(source)
                    )
                )["items"]
                operations = await rows(worker, "memory_operation")
                assert {r["purpose"] for r in operations} == {
                    "summarize",
                    "embedding_publish",
                    "embedding_search",
                }
                assert all(
                    r["state"] == "completed" and r["input_tokens"] > 0 and r["cost_micros"] == 0
                    for r in operations
                )
                assert len(calls) == len(operations)
                assert (await MemoryWorker(worker, backend, summary_endpoint=endpoint).run_once())[
                    "status"
                ] == "idle"

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("fault", ["usage", "qualification", "budget"])
def test_unknown_usage_bad_summary_and_budget_never_publish(tmp_path, fault):
    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, _, _, _):
            if fault == "budget":
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "insert into app.memory_operation(organization_id,actor_id,namespace,purpose,model,input_hash,reserved_tokens) select organization_id,%s,repeat('a',64),'summarize','fixture-summary-v1',repeat('a',64),65536 from app.memory_source cross join generate_series(1,15)",
                        (worker.actor.actor_id,),
                    )

            def transform(path, response):
                if fault == "usage" and path.endswith("chat/completions"):
                    response.pop("usage")
                return response

            with embeddings_fixture(
                summary=(lambda p: [] if fault == "qualification" else list(range(len(p)))),
                transform=transform,
            ) as (endpoint, calls):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                report = await MemoryWorker(worker, backend, summary_endpoint=endpoint).run_once()
                expected = {
                    "usage": "memory_usage_unknown",
                    "qualification": "memory_summary_invalid",
                    "budget": "memory_budget_exceeded",
                }[fault]
                assert report["code"] == expected, report
                assert await rows(owner, "memory_projection") == []
                if fault == "budget":
                    assert calls == []
                if fault == "usage":
                    op = (await rows(worker, "memory_operation"))[0]
                    assert op["state"] == "unknown" and op["cost_micros"] is None

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("cancel_task", [False, True])
def test_cancellation_during_actual_backend_does_not_accept_projection(tmp_path, cancel_task):
    async def scenario():
        async with memory_fixture() as (_, owner, worker, _, _, _, _):
            entered, release = threading.Event(), threading.Event()
            cancel = asyncio.Event()
            with embeddings_fixture(entered=entered, release=release) as (endpoint, _):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                task = asyncio.create_task(MemoryWorker(worker, backend).run_once(cancel))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    if cancel_task:
                        task.cancel()
                    else:
                        cancel.set()
                finally:
                    release.set()
                if cancel_task:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    assert (await task)["status"] == "cancelled"
                assert await rows(owner, "memory_projection") == []
                assert (await rows(worker, "memory_work_item"))[0]["state"] == "cancelled"
                assert (await rows(worker, "memory_operation"))[0]["state"] == "completed"
                assert (await MemoryWorker(worker, backend).run_once())[
                    "status"
                ] == "backup_pending"
                assert all(v["body"] is None for v in await rows(owner, "memory_version"))

    asyncio.run(scenario())


@pytest.mark.integration
def test_source_epoch_change_during_summary_rejects_publication(tmp_path):
    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, _, _, job):
            entered, release = threading.Event(), threading.Event()
            with embeddings_fixture(
                summary=lambda p: list(range(len(p))), entered=entered, release=release
            ) as (endpoint, calls):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                task = asyncio.create_task(
                    MemoryWorker(worker, backend, summary_endpoint=endpoint).run_once()
                )
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    async with await AsyncConnection.connect(url) as c:
                        await c.execute(
                            "update app.supply_case set title='Changed during summary' where id=%s",
                            (job.case_id,),
                        )
                finally:
                    release.set()
                assert (await task)["code"] == "memory_source_changed"
                assert [c["purpose"] for c in calls] == ["summarize"]
                assert await rows(owner, "memory_projection") == []

    asyncio.run(scenario())


async def consume_memory(url, runtime, job, backend):
    from pathlib import Path

    from product_api.context_session import ContextSession
    from product_api.harness import (
        Harness,
        HarnessContext,
        MemoryTraceSink,
        PiRuntimeAdapter,
        ToolDefinition,
        ToolRegistry,
    )

    async with await AsyncConnection.connect(url) as c:
        await c.execute(
            "update app.runtime_job set attempts=attempts+1,lease_expires_at=clock_timestamp()+interval '120 seconds' where id=%s",
            (job.id,),
        )
    current = replace(job, generation=job.generation + 1)
    session = ContextSession(
        runtime._tenants, runtime._identity, current, memory_backend_factory=backend
    )
    tools = ToolRegistry(
        (
            ToolDefinition(
                "supply.impact",
                lambda args: runtime.supply_impact(current, args),
                execution_store=runtime.execution_store(current),
            ),
        )
    )
    adapter = PiRuntimeAdapter(
        ("node", str(Path(__file__).resolve().parents[1] / "pi_sidecar/f42_protocol_sidecar.mjs")),
        protocol_version=3,
        context_session=session,
        timeout_seconds=30,
    )
    context = HarnessContext(
        runtime._identity.organization_id,
        current.run_id,
        current.case_id,
        "What was the prior decision?",
        invocation_id=uuid4(),
        role="history",
    )
    result = await Harness(adapter, tools).execute(context, MemoryTraceSink())
    assert result.status == "completed", result
    return session


@pytest.mark.integration
def test_actual_pi_dependency_denial_and_hard_erase_cover_later_conversation(tmp_path):
    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, original, runtime, job):
            with embeddings_fixture() as (endpoint, _):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                initial = await MemoryWorker(worker, backend).run_once()
                assert initial["status"] == "indexed", initial
                parent = (await rows(owner, "memory_version"))[0]
                session = await consume_memory(url, runtime, job, backend)
                assert session.conversation_id != original
                dependencies = await rows(worker, "memory_dependency")
                assert any(
                    d["version_id"] == parent["id"]
                    and d["conversation_id"] == session.conversation_id
                    for d in dependencies
                )
                for _ in range(10):
                    report = await MemoryWorker(worker, backend).run_once()
                    if report["status"] == "idle":
                        break
                    assert report["status"] == "indexed", report
                else:
                    pytest.fail("Derived source queue did not drain")
                versions = await rows(owner, "memory_version")
                assert len(versions) > 1
                service = MemoryService(owner, backend)
                deleted = await service.forget(parent["id"], mode="hard", include_source=True)
                assert deleted["state"] == "backup_pending"
                assert all(
                    v["body"] is None and v["status"] == "deleted"
                    for v in await rows(owner, "memory_version")
                )
                messages = await rows(worker, "context_message")
                assert messages and all(m["payload"] == {"erased": True} for m in messages)
                snapshots = await rows(worker, "context_snapshot")
                assert snapshots and all(s["projection"] == {"erased": True} for s in snapshots)
                with pytest.raises(ContextError) as error:
                    await ConversationStore(runtime._tenants, owner.actor).commit_turn(
                        session.conversation_id,
                        session.watermark,
                        "late",
                        (MessageUnit.user("Late provider result"),),
                    )
                assert "context_closed" in str(error.value)
                assert (await MemoryWorker(worker, backend).run_once())["status"] == "idle"
                adapter = backend(owner.scope((await rows(owner, "memory_source"))[0]))
                try:
                    assert all(adapter.reconcile(v["id"]) == [] for v in versions)
                finally:
                    adapter.close()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("expiry", ["version", "source"])
def test_retention_purges_versions_and_never_reprocesses_expired_source(tmp_path, expiry):
    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, _, _, _):
            with embeddings_fixture() as (endpoint, _):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                assert (await MemoryWorker(worker, backend).run_once())["status"] == "indexed"
                async with await AsyncConnection.connect(url) as c:
                    if expiry == "version":
                        await c.execute(
                            "update app.memory_version set expires_at=clock_timestamp()-interval '1 second'"
                        )
                    else:
                        await c.execute(
                            "update app.memory_source set created_at=clock_timestamp()-interval '8 days'"
                        )
                source = (await rows(owner, "memory_source"))[0]
                assert (
                    await MemoryService(owner, backend).recall(
                        "Prior decision?", scope=owner.scope(source)
                    )
                )["items"] == []
                report = await MemoryWorker(worker, backend).run_once()
                assert report["status"] == "backup_pending", report
                assert all(v["body"] is None for v in await rows(owner, "memory_version"))
                assert all(
                    m["payload"] == {"erased": True} for m in await rows(worker, "context_message")
                )
                assert (await MemoryWorker(worker, backend).run_once())["status"] == "idle"

    asyncio.run(scenario())


@pytest.mark.integration
def test_restored_sql_and_vectors_require_latest_deletion_ledger_before_reopening(tmp_path):
    import shutil

    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, _, _, _):
            storage = tmp_path / "current"
            restored_storage = tmp_path / "restored"
            with embeddings_fixture() as (endpoint, _):
                backend = lambda scope: Mem0Adapter(
                    storage, scope, embedding_url=endpoint, synthetic=True
                )
                assert (await MemoryWorker(worker, backend).run_once())["status"] == "indexed"
                version = (await rows(owner, "memory_version"))[0]
                source = (await rows(owner, "memory_source"))[0]
                shutil.copytree(storage, restored_storage)
                name = "f48_restore_" + uuid4().hex
                async with await AsyncConnection.connect(
                    make_conninfo(url, dbname="postgres"), autocommit=True
                ) as admin:
                    await admin.execute(
                        sql.SQL("create database {} template {}").format(
                            sql.Identifier(name), sql.Identifier(conninfo_to_dict(url)["dbname"])
                        )
                    )
                    try:
                        await MemoryService(owner, backend).forget(
                            version["id"], mode="hard", include_source=True
                        )
                        ledger = await owner.export_deletions()
                        path = tmp_path / "latest-deletion-ledger.json"
                        receipt = await owner.save_deletion_ledger(path)
                        assert "Prior decision" not in path.read_text()
                        assert path.stat().st_mode & 0o777 == 0o600
                        tenants = PostgresTenantRepository(make_conninfo(url, dbname=name))
                        restored_owner = MemoryStore(tenants, owner.actor)
                        restored_worker = MemoryStore(tenants, worker.actor)
                        await restored_worker.begin_recovery()
                        restored_backend = lambda scope: Mem0Adapter(
                            restored_storage, scope, embedding_url=endpoint, synthetic=True
                        )
                        restored_service = MemoryService(restored_owner, restored_backend)
                        assert (
                            await restored_service.recall(
                                "Prior decision?", scope=owner.scope(source)
                            )
                        )["items"] == []
                        with pytest.raises(MemoryBackendError, match="memory_ledger_invalid"):
                            await restored_worker.restore_deletion_ledger(path, "0" * 64)
                        with pytest.raises(MemoryBackendError, match="memory_denied"):
                            await restored_worker.finish_recovery()
                        assert (
                            await restored_worker.restore_deletion_ledger(path, receipt["sha256"])
                            > 0
                        )
                        with pytest.raises(MemoryBackendError, match="memory_denied"):
                            await restored_worker.finish_recovery()
                        assert (await MemoryWorker(restored_worker, restored_backend).run_once())[
                            "status"
                        ] == "backup_pending"
                        await restored_worker.finish_recovery()
                        assert (
                            await restored_owner.hydrate(owner.scope(source), [str(version["id"])])
                            == []
                        )
                        assert all(
                            v["body"] is None for v in await rows(restored_owner, "memory_version")
                        )
                        assert await restored_worker.restore_deletions(ledger) == 0
                        await restored_worker.finish_recovery()
                        assert (await MemoryWorker(restored_worker, restored_backend).run_once())[
                            "status"
                        ] == "idle"
                    finally:
                        await admin.execute(
                            sql.SQL("drop database {} with (force)").format(sql.Identifier(name))
                        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_unknown_erase_outcome_stays_pending_and_recovers_without_claiming_success(tmp_path):
    async def scenario():
        async with memory_fixture() as (_, owner, worker, _, _, _, _):
            with embeddings_fixture() as (endpoint, _):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                assert (await MemoryWorker(worker, backend).run_once())["status"] == "indexed"
                version = (await rows(owner, "memory_version"))[0]

                class FailAfterErase(Mem0Adapter):
                    def erase_and_rebuild(self, survivors):
                        super().erase_and_rebuild(survivors)
                        raise OSError("synthetic crash after local stores were erased")

                broken = lambda scope: FailAfterErase(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                with pytest.raises(MemoryBackendError, match="memory_erase_incomplete"):
                    await MemoryService(owner, broken).forget(
                        version["id"], mode="hard", include_source=True
                    )
                assert (await rows(owner, "memory_delete_request"))[0]["state"] == "requested"
                source = (await rows(owner, "memory_source"))[0]
                assert (
                    await MemoryService(owner, backend).recall(
                        "Prior decision?", scope=owner.scope(source)
                    )
                )["items"] == []
                assert (await MemoryWorker(worker, backend).run_once())[
                    "status"
                ] == "backup_pending"
                assert (await rows(owner, "memory_version"))[0]["body"] is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_unprocessed_source_and_unconfirmed_candidate_retention_leave_no_text(tmp_path):
    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, _, _, _):
            source = (await rows(owner, "memory_source"))[0]
            await owner.remember(
                source["object_scope"], "Prefer written risk review before delivery."
            )
            with embeddings_fixture() as (endpoint, calls):
                backend = lambda scope: Mem0Adapter(
                    tmp_path, scope, embedding_url=endpoint, synthetic=True
                )
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.memory_work_item set due_at=clock_timestamp()+interval '1 day' where source_id=%s",
                        (source["id"],),
                    )
                candidate = await MemoryWorker(worker, backend).run_once()
                assert candidate["status"] == "review_pending", candidate
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.memory_source set created_at=clock_timestamp()-interval '8 days'"
                    )
                assert (await MemoryWorker(worker, backend).run_once())[
                    "status"
                ] == "backup_pending"
                assert all(v["body"] is None for v in await rows(owner, "memory_version"))
                assert all(
                    s["explicit_text"] is None and s["state"] == "deleted"
                    for s in await rows(owner, "memory_source")
                )
                assert calls == []

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("change", ["epoch", "revocation", "generation"])
def test_model_preflight_rejects_changed_authority_before_any_http_request(change):
    async def scenario():
        async with memory_fixture() as (url, owner, worker, _, _, _, job):
            work = await worker.claim()
            source, messages = await worker.source(work["source_id"])
            async with await AsyncConnection.connect(url) as c:
                if change == "epoch":
                    await c.execute(
                        "update app.supply_case set title='Changed before reservation' where id=%s",
                        (job.case_id,),
                    )
                elif change == "revocation":
                    await c.execute(
                        "update app.organization_member set role='maintainer' where organization_id=%s and user_id=%s",
                        (owner.actor.organization_id, owner.actor.actor_id),
                    )
                else:
                    await c.execute(
                        "update app.memory_work_item set generation=generation+1 where id=%s",
                        (work["id"],),
                    )
            with embeddings_fixture(summary=lambda p: list(range(len(p)))) as (endpoint, calls):
                port = LocalModelPort(
                    endpoint, worker.meter(owner.scope(source), work, epoch=source["source_epoch"])
                )
                with pytest.raises(
                    MemoryBackendError, match="memory_source_changed|memory_lease_lost"
                ):
                    await asyncio.to_thread(port.summarize, summarize(source, messages))
                assert calls == []
                assert await rows(worker, "memory_operation") == []

    asyncio.run(scenario())
