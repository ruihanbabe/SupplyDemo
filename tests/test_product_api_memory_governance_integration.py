"""M2 real SQL/SDK: source-qualified relations and one legacy reader at a time."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from test_product_api_mem0_backend_integration import embeddings_fixture
from test_product_api_memory_lifecycle_integration import MIGRATIONS, rows
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.context_store import ContextActor
from product_api.mem0_adapter import Mem0Adapter, MemoryBackendError
from product_api.memory_governance import MemoryGovernance
from product_api.memory_runtime import MemoryService, MemoryWorker
from product_api.memory_store import MemoryStore

M2 = MIGRATIONS


async def setup(url, tmp_path, endpoint):
    org, user, _, _, runtime, _ = await seed_tool_job(url)
    owner = MemoryStore(runtime._tenants, ContextActor(org, user))
    worker = MemoryStore(runtime._tenants, runtime._identity)
    backend = lambda scope: Mem0Adapter(tmp_path, scope, embedding_url=endpoint, synthetic=True)
    return owner, worker, backend


async def preference(owner, worker, backend, scope, body, **qualifiers):
    source = await owner.remember(scope, body)
    report = await MemoryWorker(worker, backend).run_once()
    assert report["status"] == "review_pending", report
    version = next(
        v for v in await rows(owner, "memory_version") if str(v["id"]) == report["versionId"]
    )
    if qualifiers:
        await MemoryGovernance(owner).qualify(version["id"], **qualifiers)
    await owner.confirm(version["id"])
    report = await MemoryWorker(worker, backend).run_once()
    assert report["status"] == "indexed", report
    return source, version["id"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "topic", ["customer preference", "opportunity preference", "decision episode", "task episode"]
)
def test_entity_qualifiers_and_multisource_relations(tmp_path, topic):
    async def scenario():
        async with tool_database(*M2) as url:
            with embeddings_fixture() as (endpoint, _):
                owner, worker, backend = await setup(url, tmp_path, endpoint)
                governance = MemoryGovernance(owner)
                entity = '{"customerId":"' + str(uuid4()) + '","displayName":"Same Name"}'
                other_entity = '{"customerId":"' + str(uuid4()) + '","displayName":"Same Name"}'
                when = datetime.now(UTC) - timedelta(days=2)
                qualifiers = {
                    "event_at": when,
                    "valid_from": when,
                    "valid_to": when + timedelta(days=1),
                    "conditions": "Only after sample approval",
                }
                body = f"Prior {topic}: NOT approved because samples failed; only after sample approval. Historical BOM and expired quote are not current facts."
                a, first = await preference(owner, worker, backend, entity, body, **qualifiers)
                b, second = await preference(owner, worker, backend, entity, body, **qualifiers)
                _, other = await preference(
                    owner, worker, backend, other_entity, body, **qualifiers
                )
                await governance.relate(first, second, "duplicate_of")
                await governance.relate(first, second, "duplicate_of")
                supports = [
                    e for e in await rows(owner, "memory_evidence") if e["version_id"] == first
                ]
                assert {e["source_id"] for e in supports} == {a, b}
                with pytest.raises(MemoryBackendError, match="memory_denied"):
                    await governance.relate(first, other, "duplicate_of")
                _, contradiction = await preference(
                    owner,
                    worker,
                    backend,
                    entity,
                    "Prior decision: approved; only after sample approval.",
                    **qualifiers,
                )
                await governance.relate(first, contradiction, "contradicts")
                service = MemoryService(owner, backend)
                source, _ = await owner.source(a)
                result = await service.recall("Prior preference?", scope=owner.scope(source))
                assert str(other) not in {i["versionId"] for i in result["items"]}
                item = next(i for i in result["items"] if i["versionId"] == str(first))
                assert len(item["sources"]) == 2
                assert item["governance"]["valid_to"] == qualifiers["valid_to"].isoformat()
                assert any(r["kind"] == "contradicts" for r in item["governance"]["relations"])
                assert (
                    item["governance"]["authority"]
                    == "historical_reference_not_current_business_fact"
                )
                assert len(await service.read(first, scope=owner.scope(source), level="L2")) == 2
                with pytest.raises(MemoryBackendError, match="memory_denied"):
                    await governance.qualify(first, conditions="Changed in place")
                await governance.relate(contradiction, first, "derived_from")
                await governance.relate(contradiction, second, "merged_from")
                await governance.relate(contradiction, first, "supersedes")
                assert not await owner.hydrate(owner.scope(source), [str(first)])
                request = await service.forget(second, mode="hard", include_source=True)
                assert request["state"] == "backup_pending"
                assert not await owner.hydrate(owner.scope(source), [str(first), str(second)])
                erased = [
                    v for v in await rows(owner, "memory_version") if v["id"] in {first, second}
                ]
                assert all(v["body"] is None for v in erased)
                assert (await rows(owner, "memory_delete_request"))[0]["state"] == "backup_pending"

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("withdraw", [False, True])
def test_legacy_export_revalidate_reconcile_switch_and_safe_rollback(tmp_path, withdraw):
    async def scenario():
        async with tool_database(*M2) as url:
            with embeddings_fixture() as (endpoint, _):
                owner, worker, backend = await setup(url, tmp_path, endpoint)
                governance = MemoryGovernance(owner)
                doc, knowledge, legacy = [uuid4() for _ in range(3)]
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "insert into app.knowledge_document(id,organization_id,title,document_kind,visibility,source_kind,created_by) values(%s,%s,'synthetic policy','business_policy','organization','synthetic',%s)",
                        (doc, owner.actor.organization_id, owner.actor.actor_id),
                    )
                    await c.execute(
                        "insert into app.knowledge_document_version(id,organization_id,document_id,version_number,version_label,source_revision,content_hash,content_bytes,published_by) values(%s,%s,%s,1,'v1','synthetic',repeat('a',64),50,%s)",
                        (knowledge, owner.actor.organization_id, doc, owner.actor.actor_id),
                    )
                    await c.execute(
                        "insert into app.long_term_memory(id,organization_id,content,source_ref,source_kind,source_version_id,status,proposed_by,confirmed_by) values(%s,%s,'{\"preference\":\"Do not promise delivery\"}','{\"synthetic\":true}','deterministic',%s,'confirmed',%s,%s)",
                        (
                            legacy,
                            owner.actor.organization_id,
                            knowledge,
                            owner.actor.actor_id,
                            owner.actor.actor_id,
                        ),
                    )
                assert (await governance.export_legacy())[0]["id"] == legacy
                assert len(await governance.route()) == 1
                with pytest.raises(MemoryBackendError, match="memory_denied"):
                    await governance.route("governed")
                source = await governance.import_legacy(legacy, "synthetic:customer-identity")
                assert (
                    await governance.import_legacy(legacy, "synthetic:customer-identity") == source
                )
                result = await MemoryWorker(worker, backend).run_once()
                assert result["status"] == "review_pending", result
                version = result["versionId"]
                await governance.reconcile_legacy(version)
                report = await MemoryWorker(worker, backend).run_once()
                assert report["status"] == "indexed", report
                governed = await governance.route("governed")
                assert (
                    len(governed) == 1
                    and governed[0]["id"] == version
                    and "content" not in governed[0]
                )
                if withdraw:
                    async with await AsyncConnection.connect(url) as c:
                        await c.execute(
                            "insert into app.knowledge_document_withdrawal(document_id,organization_id,reason,withdrawn_by) values(%s,%s,'synthetic withdrawal',%s)",
                            (doc, owner.actor.organization_id, owner.actor.actor_id),
                        )
                    with pytest.raises(MemoryBackendError, match="memory_source_denied"):
                        await owner.source(source)
                    assert await governance.route() == []
                    with pytest.raises(MemoryBackendError):
                        await governance.import_legacy(legacy, "synthetic:customer-identity")
                await owner.forget(version, "hard", True)
                assert await governance.route("legacy") == []
                assert await governance.route("governed") == []
                with pytest.raises(MemoryBackendError):
                    await governance.import_legacy(legacy, "synthetic:customer-identity")
                assert (await MemoryWorker(worker, backend).run_once())[
                    "status"
                ] == "backup_pending"
                legacy_rows = await governance.export_legacy()
                assert legacy_rows[0]["content"] is None
                assert (await rows(owner, "long_term_memory"))[0]["content"] == {"erased": True}
                assert legacy_rows[0]["status"] == "revoked"

    asyncio.run(scenario())
