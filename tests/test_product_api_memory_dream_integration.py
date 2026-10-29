"""M3 lossless ablation, durable recovery and pending/accepted deletion closure."""

import asyncio
import shutil

import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_product_api_mem0_backend_integration import embeddings_fixture
from test_product_api_memory_governance_integration import M2, preference, setup
from test_product_api_memory_lifecycle_integration import rows
from test_product_api_tool_runtime_integration import tool_database

from product_api.mem0_adapter import Mem0Adapter, MemoryBackendError, MemoryScope
from product_api.memory_dream import MemoryDream
from product_api.memory_runtime import MemoryService, MemoryWorker
from product_api.memory_store import MemoryStore
from product_api.tenant import PostgresTenantRepository


@pytest.mark.integration
@pytest.mark.parametrize("pending", [False, True])
def test_dream_lossless_ablation_leases_roots_and_backup_replay(tmp_path, pending):
    from uuid import uuid4

    async def scenario():
        async with tool_database(*M2) as url:
            storage = tmp_path / "live"
            with embeddings_fixture() as (endpoint, calls):
                owner, worker, backend = await setup(url, storage, endpoint)
                business_before = await rows(worker, "runtime_job")
                cases_before = await rows(owner, "supply_case")
                dream = MemoryDream(worker)
                governance = MemoryDream(owner)
                scope = MemoryScope(
                    owner.actor.organization_id, owner.actor.actor_id, "synthetic:entity-id"
                )
                bodies = [
                    f"Prior task {i}: NOT approved on 2026-10-28 because test {i} failed; only review after sample approval."
                    for i in range(6)
                ]
                versions = []
                for body in bodies:
                    _, version = await preference(owner, worker, backend, scope.object_scope, body)
                    versions.append(version)
                before_calls = len(calls)
                assert await dream.schedule(scope) is None
                assert len(calls) == before_calls
                baseline = await MemoryService(owner, backend).recall(
                    "Prior task decisions?", scope=scope
                )
                baseline_text = "\n".join(i["body"] for i in baseline["items"])
                baseline_coverage = sum(body in baseline_text for body in bodies)
                assert baseline_coverage == 5
                source = await dream.schedule(scope, enabled=True)
                assert source and await dream.schedule(scope, enabled=True) == source
                batches = await rows(owner, "memory_dream_batch")
                assert len(batches) == 1 and batches[0]["evaluation"]["lossless"] is True
                old_work = await worker.claim()
                assert old_work["source_id"] == source
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.memory_work_item set lease_until=clock_timestamp()-interval '1 second' where id=%s",
                        (old_work["id"],),
                    )
                derived = None
                if not pending:
                    result = await MemoryWorker(worker, backend).run_once()
                    assert result["status"] == "indexed", result
                    derived = result["versionId"]
                    with pytest.raises(MemoryBackendError, match="memory_lease_lost"):
                        await worker.accept(old_work, None, "Old generation must not publish")
                    assert len(await governance.independent_sources(derived)) == 6
                    for version in versions:
                        await governance.cool(version)
                    consolidated = await MemoryService(owner, backend).recall(
                        "Prior task decisions?", scope=scope
                    )
                    text = "\n".join(i["body"] for i in consolidated["items"])
                    assert sum(body in text for body in bodies) == 6 > baseline_coverage
                    item = next(i for i in consolidated["items"] if i["versionId"] == derived)
                    assert item["trust"] == "generated_reference"
                    assert all(b in item["body"] for b in bodies)
                    assert len({s["id"] for s in item["sources"]}) == 7
                    lesson_source = await governance.propose_lesson(derived, bodies[0])
                    assert await governance.propose_lesson(derived, bodies[0]) == lesson_source
                    lesson = await MemoryWorker(worker, backend).run_once()
                    assert lesson["status"] == "review_pending"
                    candidate = next(
                        v
                        for v in await rows(owner, "memory_version")
                        if str(v["id"]) == lesson["versionId"]
                    )
                    assert candidate["summary_manifest"]["independentSources"] == 6
                    assert candidate["trust"] == "unconfirmed" and candidate["confirmed_by"] is None
                    assert len(await governance.independent_sources(derived)) == 6
                    assert not await owner.hydrate(scope, [lesson["versionId"]])
                    count = len(calls)
                    assert await dream.schedule(scope, enabled=True) == source
                    assert (await MemoryWorker(worker, backend).run_once())["status"] == "idle"
                    assert len(calls) == count
                assert await rows(worker, "runtime_job") == business_before
                assert await rows(owner, "supply_case") == cases_before
                if not pending:
                    await preference(
                        owner,
                        worker,
                        backend,
                        scope.object_scope,
                        "Prior new independent task: NOT approved; review after samples.",
                    )
                    count = len(calls)
                    assert (
                        await dream.schedule(scope, enabled=True) is None
                    )  # Same day, fewer than 50 new inputs.
                    assert len(calls) == count
                # Back up actual database + vector/history files with old pending or accepted work.
                restored = tmp_path / "restored"
                shutil.copytree(storage, restored)
                name = "f48_dream_restore_" + uuid4().hex
                async with await AsyncConnection.connect(
                    make_conninfo(url, dbname="postgres"), autocommit=True
                ) as admin:
                    await admin.execute(
                        sql.SQL("create database {} template {}").format(
                            sql.Identifier(name), sql.Identifier(conninfo_to_dict(url)["dbname"])
                        )
                    )
                    try:
                        request = await owner.forget(versions[0], "hard", True)
                        count = len(calls)
                        if pending:
                            result = await MemoryWorker(worker, backend).run_once()
                            assert result["status"] == "backup_pending", result
                            assert (
                                len(calls) == count + 5
                            )  # Rebuild only the five legal unrelated originals.
                        else:
                            assert (await MemoryWorker(worker, backend).run_once())[
                                "status"
                            ] == "backup_pending"
                        assert (await owner.deletion(request))["state"] == "backup_pending"
                        source_row = next(
                            s for s in await rows(owner, "memory_source") if s["id"] == source
                        )
                        assert (
                            source_row["explicit_text"] is None and source_row["state"] == "deleted"
                        )
                        ledger_path = tmp_path / "latest-ledger.json"
                        receipt = await owner.save_deletion_ledger(ledger_path)
                        tenants = PostgresTenantRepository(make_conninfo(url, dbname=name))
                        restored_owner = MemoryStore(tenants, owner.actor)
                        restored_worker = MemoryStore(tenants, worker.actor)
                        restored_backend = lambda domain: Mem0Adapter(
                            restored, domain, embedding_url=endpoint, synthetic=True
                        )
                        await restored_worker.begin_recovery()
                        await restored_worker.restore_deletion_ledger(
                            ledger_path, receipt["sha256"]
                        )
                        with pytest.raises(MemoryBackendError, match="memory_denied"):
                            await restored_worker.finish_recovery()
                        drained = 0
                        while await restored_worker.pending_deletion():
                            assert (
                                await MemoryWorker(restored_worker, restored_backend).run_once()
                            )["status"] == "backup_pending"
                            drained += 1
                            assert drained <= 10
                        assert drained > 0
                        await restored_worker.finish_recovery()
                        assert not await restored_owner.hydrate(scope, [str(versions[0])])
                        if derived:
                            assert not await restored_owner.hydrate(scope, [derived])
                        restored_source = next(
                            s
                            for s in await rows(restored_owner, "memory_source")
                            if s["id"] == source
                        )
                        assert restored_source["explicit_text"] is None
                        count = len(calls)
                        assert (await MemoryWorker(restored_worker, restored_backend).run_once())[
                            "status"
                        ] == "idle"
                        assert len(calls) == count
                    finally:
                        await admin.execute(
                            sql.SQL("drop database {} with (force)").format(sql.Identifier(name))
                        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_task_terminal_flushes_retrospective_and_no_gain_does_not_schedule(tmp_path):
    from product_api.context_store import ConversationStore, MessageUnit

    async def scenario():
        async with tool_database(*M2) as url:
            with embeddings_fixture() as (endpoint, calls):
                owner, worker, backend = await setup(url, tmp_path, endpoint)
                conversations = ConversationStore(owner.tenants, owner.actor)
                job = (await rows(worker, "runtime_job"))[0]
                conversation = await conversations.open(job["id"], "retrospective")
                await owner.enable(conversation, synthetic=True)
                await conversations.commit_turn(
                    conversation,
                    0,
                    "terminal-review",
                    (
                        MessageUnit.user(
                            "Prior task: NOT approved because evidence is missing; review after samples."
                        ),
                    ),
                )
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.runtime_job set status='completed' where id=%s", (job["id"],)
                    )
                saved = (await rows(owner, "context_conversation"))[0]
                assert saved["state"] == "closed"
                result = await MemoryWorker(worker, backend).run_once()
                assert result["status"] == "indexed", result
                version = (await rows(owner, "memory_version"))[0]
                assert (
                    version["trust"] == "generated_reference" and "NOT approved" in version["body"]
                )
                source, _ = await owner.source((await rows(owner, "memory_source"))[0]["id"])
                count = len(calls)
                assert await MemoryDream(worker).schedule(owner.scope(source), enabled=True) is None
                assert len(calls) == count
                assert await rows(owner, "memory_dream_batch") == []
                lesson = await MemoryDream(owner).propose_lesson(
                    version["id"], "NOT approved because evidence is missing"
                )
                assert lesson
                result = await MemoryWorker(worker, backend).run_once()
                assert result["status"] == "review_pending"
                candidate = next(
                    v
                    for v in await rows(owner, "memory_version")
                    if str(v["id"]) == result["versionId"]
                )
                assert candidate["summary_manifest"]["independentSources"] == 1
                with pytest.raises(MemoryBackendError, match="memory_source_denied"):
                    await MemoryDream(owner).propose_lesson(
                        version["id"], "Always approve deliveries"
                    )

    asyncio.run(scenario())
