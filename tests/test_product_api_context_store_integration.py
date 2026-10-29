"""C1 sources/checkpoints verified only in automatically dropped cloned databases."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.context_store import (
    ContextActor,
    ContextError,
    ConversationStore,
    EvidenceRef,
    MessageUnit,
    TranscriptMessage,
    WorkingState,
)
from product_api.harness import ToolDefinition
from product_api.tool_registry import TOOL_REGISTRY
from product_api.tool_runtime import CallIdentity, ToolRuntime


@pytest.mark.integration
def test_context_commands_pairing_isolation_commit_and_recovery():
    async def scenario():
        async with tool_database(
            "0031_f51_tool_resources.sql", "0032_f52_context_sources.sql"
        ) as url:
            org, user, _worker, _, runtime, job = await seed_tool_job(url)
            owner = ConversationStore(runtime._tenants, ContextActor(org, user))
            store = ConversationStore(runtime._tenants, runtime._identity)
            conversation = await owner.open(job.id, "supply_risk")
            assert conversation == await owner.open(job.id, "supply_risk")
            unit = MessageUnit.user("Investigate PART-A; arrival remains unconfirmed.")
            assert await owner.commit_turn(conversation, 0, "user-1", (unit,)) == 1
            assert await owner.commit_turn(conversation, 0, "user-1", (unit,)) == 1
            with pytest.raises(ContextError, match="context_idempotency_conflict"):
                await owner.commit_turn(conversation, 0, "user-1", (MessageUnit.user("changed"),))
            execution = runtime.execution_store(job)
            tools = ToolRuntime(
                (
                    ToolDefinition(
                        "supply.impact",
                        lambda args: runtime.supply_impact(job, args),
                        execution_store=execution,
                    ),
                ),
                TOOL_REGISTRY.bind(("supply.impact",)),
            )
            identity = CallIdentity(uuid4(), "call|raw")
            prepared = await tools.prepare(identity, "supply.impact", {"partNumber": "PART-A"})
            result = await tools.execute(prepared)
            head = TranscriptMessage(
                role="assistant",
                invocation_id=identity.invocation_id,
                batch_id=1,
                payload={
                    "toolCalls": [
                        {
                            "id": identity.call_id,
                            "name": "supply.impact",
                            "arguments": {"partNumber": "PART-A"},
                        }
                    ]
                },
            )
            tail = TranscriptMessage(
                role="tool_result",
                invocation_id=identity.invocation_id,
                batch_id=1,
                index=0,
                call_id=identity.call_id,
                payload={
                    "status": result.status,
                    "code": result.code,
                    "resultRef": result.result_ref,
                },
            )
            pair = MessageUnit(messages=(head, tail))
            with pytest.raises(ContextError, match="context_denied"):
                await owner.commit_turn(conversation, 1, "forged", (pair,))
            # Source payload is intentionally only a result reference, not a second copy.
            tail = tail.model_copy(
                update={"payload": {"status": "ok", "code": None, "resultRef": result.result_ref}}
            )
            pair = MessageUnit(messages=(head, tail))
            assert await store.commit_turn(conversation, 1, "tools", (pair,), job=job) == 3
            page = await owner.read_range(conversation, after=2, limit=1)
            assert [m["ordinal"] for m in page] == [2, 3]
            assert page[1]["payload"]["resultRef"] == result.result_ref
            ref = EvidenceRef.model_validate_json(__import__("json").dumps(result.result_ref))
            state = WorkingState(
                goal="Investigate",
                accepted_evidence=(ref,),
                unknowns=("arrival unconfirmed",),
                next_step="Check current ETA",
            )
            assert await store.checkpoint(conversation, 0, 3, state, job) == 1
            with pytest.raises(ContextError, match="context_version_conflict"):
                await store.checkpoint(conversation, 0, 3, state, job)
            rebuilt = await store.rebuild(conversation, job)
            assert (
                rebuilt["state"] == state
                and rebuilt["resumeMode"] == "new_invocation"
                and rebuilt["memoryStatus"] == "disabled"
            )
            peer = uuid4()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
                    (org, peer),
                )
            with pytest.raises(ContextError, match="context_denied"):
                await ConversationStore(runtime._tenants, ContextActor(org, peer)).read_range(
                    conversation
                )
            foreign = uuid4()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "insert into app.organization(id,slug,display_name) values(%s,%s,'foreign')",
                    (foreign, foreign.hex),
                )
                await c.execute(
                    "insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'organization_admin')",
                    (foreign, user),
                )
            with pytest.raises(ContextError, match="context_denied"):
                await ConversationStore(runtime._tenants, ContextActor(foreign, user)).read_range(
                    conversation
                )
            # New generation may rebuild validated state, but cannot accept an old writer.
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.runtime_job set attempts=attempts+1 where id=%s", (job.id,)
                )
            with pytest.raises(ContextError, match="lease_lost"):
                await store.checkpoint(conversation, 1, 3, state, job)
            assert (await store.rebuild(conversation, replace(job, generation=job.generation + 1)))[
                "state"
            ] == state
            results = await asyncio.gather(
                *(
                    owner.commit_turn(
                        conversation, 3, "race-" + str(i), (MessageUnit.user("next " + str(i)),)
                    )
                    for i in range(2)
                ),
                return_exceptions=True,
            )
            assert sum(r == 4 for r in results) == 1
            assert (
                sum(
                    isinstance(r, ContextError) and r.code == "context_watermark_conflict"
                    for r in results
                )
                == 1
            )
            await owner.close(conversation, 4)
            await owner.close(conversation, 4)
            assert await owner.commit_turn(conversation, 0, "user-1", (unit,)) == 1
            with pytest.raises(ContextError, match="context_closed"):
                await owner.commit_turn(conversation, 4, "late", (MessageUnit.user("late"),))
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "update app.organization_member set role='maintainer' where user_id=%s", (user,)
                )
            with pytest.raises(ContextError, match="context_denied"):
                await owner.read_range(conversation)

    asyncio.run(scenario())


async def recorded_tool_unit(runtime, job):
    tools = ToolRuntime(
        (
            ToolDefinition(
                "supply.impact",
                lambda args: runtime.supply_impact(job, args),
                execution_store=runtime.execution_store(job),
            ),
        ),
        TOOL_REGISTRY.bind(("supply.impact",)),
    )
    identity = CallIdentity(uuid4(), "rollback|call")
    prepared = await tools.prepare(identity, "supply.impact", {"partNumber": "PART-A"})
    outcome = await tools.execute(prepared)
    head = TranscriptMessage(
        role="assistant",
        invocation_id=identity.invocation_id,
        batch_id=1,
        payload={
            "toolCalls": [
                {
                    "id": identity.call_id,
                    "name": "supply.impact",
                    "arguments": {"partNumber": "PART-A"},
                }
            ]
        },
    )
    tail = TranscriptMessage(
        role="tool_result",
        invocation_id=identity.invocation_id,
        batch_id=1,
        index=0,
        call_id=identity.call_id,
        payload={"status": "ok", "code": None, "resultRef": outcome.result_ref},
    )
    return MessageUnit(messages=(head, tail)), outcome


@pytest.mark.integration
def test_invalid_tail_rolls_back_whole_commit_and_checkpoint():
    import copy

    from psycopg.types.json import Jsonb

    async def scenario():
        async with tool_database(
            "0031_f51_tool_resources.sql", "0032_f52_context_sources.sql"
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = ConversationStore(runtime._tenants, ContextActor(org, user))
            store = ConversationStore(runtime._tenants, runtime._identity)
            conversation = await owner.open(job.id, "supply_risk")
            pair, result = await recorded_tool_unit(runtime, job)
            first = MessageUnit.user("No delivery commitment confirmed.")
            for field, value in (("fragmentHash", "0" * 64), ("locator", "payload")):
                serialized = [
                    first.model_dump(mode="json", by_alias=True),
                    pair.model_dump(mode="json", by_alias=True),
                ]
                serialized[1]["messages"][1]["payload"]["resultRef"][field] = value

                async def write(c, field=field, serialized=serialized):
                    await c.execute(
                        "select app.context_commit(%s,%s,%s,%s,0,%s,%s)",
                        (
                            conversation,
                            job.id,
                            runtime._identity.owner,
                            job.generation,
                            field,
                            Jsonb(serialized),
                        ),
                    )

                with pytest.raises(ContextError, match="context_invalid_evidence"):
                    await store._transaction(write)
                assert await owner.read_range(conversation) == []
            assert await owner.commit_turn(conversation, 0, "valid", (first,)) == 1
            assert await store.commit_turn(conversation, 1, "valid-pair", (pair,), job=job) == 3
            invalid = copy.deepcopy(result.result_ref)
            invalid["locator"] = "payload"

            async def invalid_checkpoint(c):
                await c.execute(
                    "select app.context_checkpoint(%s,%s,%s,%s,0,3,%s)",
                    (
                        conversation,
                        job.id,
                        runtime._identity.owner,
                        job.generation,
                        Jsonb({"goal": "test", "acceptedEvidence": [invalid]}),
                    ),
                )

            with pytest.raises(ContextError, match="context_invalid_evidence"):
                await store._transaction(invalid_checkpoint)
            assert (await store.rebuild(conversation, job))["version"] == 0

    asyncio.run(scenario())


@pytest.mark.integration
def test_uncommitted_process_death_releases_transaction_without_advancing_watermark():
    import json
    import sys

    program = r"""
import json,sys,psycopg
from psycopg.types.json import Jsonb
p=json.loads(sys.stdin.readline())
with psycopg.connect(p['url']) as c:
 c.execute('set local role supplyagent_api')
 c.execute('select app.set_request_context(%s,%s)',(p['user'],p['org']))
 c.execute('select app.context_commit(%s,null,null,null,0,%s,%s)',(p['conversation'],'crashed',Jsonb(p['units'])))
 print('staged',flush=True)
 sys.stdin.read()
"""

    async def scenario():
        async with tool_database(
            "0031_f51_tool_resources.sql", "0032_f52_context_sources.sql"
        ) as url:
            org, user, _, _, runtime, job = await seed_tool_job(url)
            owner = ConversationStore(runtime._tenants, ContextActor(org, user))
            conversation = await owner.open(job.id, "supply_risk")
            unit = MessageUnit.user("durable after retry")
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                program,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            try:
                child.stdin.write(
                    (
                        json.dumps(
                            {
                                "url": url,
                                "org": str(org),
                                "user": str(user),
                                "conversation": str(conversation),
                                "units": [unit.model_dump(mode="json", by_alias=True)],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await child.stdin.drain()
                assert await asyncio.wait_for(child.stdout.readline(), 5) == b"staged\n"
                child.kill()
                await child.wait()
                assert await asyncio.wait_for(owner.read_range(conversation), 5) == []
                assert await owner.commit_turn(conversation, 0, "crashed", (unit,)) == 1
                assert await owner.commit_turn(conversation, 0, "crashed", (unit,)) == 1
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

    asyncio.run(scenario())


@pytest.mark.integration
def test_explicit_resume_checks_object_scope_and_revalidates_prior_evidence():
    from product_api.runtime import PostgresRuntimeStore, WorkerIdentity

    async def scenario():
        async with tool_database(
            "0031_f51_tool_resources.sql", "0032_f52_context_sources.sql"
        ) as url:
            org, user, _, case, runtime, job = await seed_tool_job(url)
            owner = ConversationStore(runtime._tenants, ContextActor(org, user))
            store = ConversationStore(runtime._tenants, runtime._identity)
            conversation = await owner.open(job.id, "supply_risk")
            pair, result = await recorded_tool_unit(runtime, job)
            assert await store.commit_turn(conversation, 0, "old", (pair,), job=job) == 2
            import json

            ref = EvidenceRef.model_validate_json(json.dumps(result.result_ref))
            await store.checkpoint(
                conversation,
                0,
                2,
                WorkingState(goal="Continue investigation", accepted_evidence=(ref,)),
                job,
            )
            author = PostgresRuntimeStore(runtime._tenants, WorkerIdentity(org, user, "author"))
            await author.begin_supply(case, "PART-A", uuid4().hex)
            same = await runtime.claim_next(60)
            assert same.id != job.id
            assert (await store.rebuild(conversation, same))["state"].accepted_evidence == (ref,)
            other_case = uuid4()
            async with await AsyncConnection.connect(url) as c:
                await c.execute(
                    "insert into app.supply_case(id,organization_id,title,status,severity,scope) values(%s,%s,'other','open','high','{\"partNumbers\":[\"PART-A\"]}')",
                    (other_case, org),
                )
            await author.begin_supply(other_case, "PART-A", uuid4().hex)
            other = await runtime.claim_next(60)
            with pytest.raises(ContextError, match="context_scope_changed"):
                await store.rebuild(conversation, other)

    asyncio.run(scenario())
