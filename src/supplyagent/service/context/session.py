"""F52 assembly of the durable Context interfaces for one Pi invocation."""

from __future__ import annotations

import copy
import json
import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from supplyagent.sdk.context.compiler import (
    ContextCompiler,
    ContextPolicy,
    canonical,
    complete_units,
    digest,
    text_content,
)
from supplyagent.sdk.tools.registry import ToolUnavailableError
from supplyagent.service.persistence.context_store import (
    ContextError,
    ConversationStore,
    EvidenceRef,
    MessageUnit,
    TranscriptMessage,
    WorkingState,
)


class ContextSession:
    def __init__(
        self,
        tenants,
        identity,
        job,
        policy=None,
        *,
        conversation_id=None,
        memory=None,
        memory_backend_factory=None,
    ):
        self.store = ConversationStore(tenants, identity)
        self.identity, self.job = identity, job
        self.policy = policy or ContextPolicy.load()
        self.compiler = ContextCompiler(self.policy, memory)
        self.conversation_id = conversation_id
        self.watermark = 0
        self.captured = []
        self.outcomes = {}
        self.batches = {}
        self.ordinal = 0
        self.memory_backend_factory = memory_backend_factory
        self.memory_scope = None

    async def start(self, context, invocation, tools):
        self.context, self.invocation, self.tools = context, invocation, tools
        role = context.role or "standalone"
        if self.conversation_id is None:
            self.conversation_id = await self.store.open(
                self.job.id, f"{role}_g{self.job.generation}"
            )
        rebuilt = await self.store.rebuild(self.conversation_id, self.job)
        stored_role = rebuilt["conversation"]["role"]
        if stored_role != role and not re.fullmatch(
            re.escape(role) + r"_g[1-9][0-9]*", stored_role
        ):
            raise ContextError("context_scope_changed")
        if self.memory_backend_factory is not None:
            from supplyagent.service.memory.mem0_adapter import MemoryScope
            from supplyagent.service.memory.runtime import MemoryService
            from supplyagent.service.memory.store import MemoryStore
            from supplyagent.service.persistence.context_store import ContextActor

            async def bind(c):
                await c.execute(
                    "select app.memory_enable_worker(%s,%s,%s,%s)",
                    (self.conversation_id, self.job.id, self.identity.owner, self.job.generation),
                )
                return await (
                    await c.execute(
                        "select owner_id,jsonb_build_object('scope',scope,'role',regexp_replace(role,'_g[0-9]+$',''))::text as object_scope from app.context_conversation where id=%s",
                        (self.conversation_id,),
                    )
                ).fetchone()

            bound = await self.store._transaction(bind)
            self.memory_scope = MemoryScope(
                self.identity.organization_id, bound["owner_id"], bound["object_scope"]
            )
            self.compiler.memory = MemoryService(
                MemoryStore(
                    self.store.tenants,
                    ContextActor(self.identity.organization_id, bound["owner_id"]),
                ),
                self.memory_backend_factory,
            )
        self.watermark = rebuilt["conversation"]["committed_watermark"]
        # Default invocations start fresh; explicit reuse must be append-only and never replay tools.
        self.working = (
            rebuilt["state"].model_dump(mode="json", by_alias=True) if rebuilt["state"] else None
        )

    def remember_batch(self, batch, outcomes):
        for (identity, name, args), outcome in zip(batch, outcomes):
            self.outcomes[identity.call_id] = outcome
            self.batches[identity.call_id] = (identity, name, args)

    async def capture(self, messages):
        if len(messages) < len(self.captured) or digest(messages[: len(self.captured)]) != digest(
            self.captured
        ):
            raise ContextError("context_history_changed")
        fresh = messages[len(self.captured) :]
        if not fresh:
            return
        units = []
        for group in complete_units(fresh):
            head = group[0]
            if head["role"] == "user":
                if self.captured or units or text_content(head["content"]) != self.context.task:
                    raise ContextError("context_history_changed")
                units.append(MessageUnit.user(text_content(head["content"])))
            else:
                calls = [b for b in head["content"] if b["type"] == "toolCall"]
                if calls:
                    normalized = []
                    results = []
                    batch_id = None
                    model_key = None
                    for index, (call, result) in enumerate(zip(calls, group[1:])):
                        entry = self.batches.get(call["id"])
                        outcome = self.outcomes.get(call["id"])
                        if not entry or not outcome:
                            raise ContextError("context_invalid_evidence")
                        identity, name, args = entry
                        try:
                            canonical_name = self.tools.binding.resolve(call["name"]).name
                        except ToolUnavailableError:
                            canonical_name = call["name"]
                        if (
                            canonical_name != name
                            or args != call["arguments"]
                            or identity.index != index
                        ):
                            raise ContextError("context_history_changed")
                        body = json.loads(text_content(result["content"]))
                        if body != outcome.payload and body != {
                            "citation": outcome.result_ref,
                            "omitted": True,
                            "readRequired": True,
                        }:
                            raise ContextError("context_invalid_evidence")
                        if bool(result.get("isError")) != (outcome.status != "ok"):
                            raise ContextError("context_invalid_evidence")
                        normalized.append({"id": identity.call_id, "name": name, "arguments": args})
                        results.append(
                            TranscriptMessage(
                                role="tool_result",
                                invocation_id=self.invocation,
                                model_call_key=identity.model_call_key,
                                batch_id=identity.batch_id,
                                call_id=identity.call_id,
                                index=index,
                                payload={
                                    "status": outcome.status,
                                    "code": outcome.code,
                                    "resultRef": outcome.result_ref,
                                },
                            )
                        )
                        batch_id, model_key = identity.batch_id, identity.model_call_key
                    source = TranscriptMessage(
                        role="assistant",
                        invocation_id=self.invocation,
                        model_call_key=model_key,
                        batch_id=batch_id,
                        payload={
                            "toolCalls": normalized,
                            "content": [b for b in head["content"] if b["type"] != "toolCall"],
                        },
                    )
                    units.append(MessageUnit(messages=(source, *results)))
                else:
                    units.append(
                        MessageUnit(
                            messages=(
                                TranscriptMessage(
                                    role="assistant",
                                    invocation_id=self.invocation,
                                    payload={
                                        "content": head["content"],
                                        "stopReason": head.get("stopReason"),
                                    },
                                ),
                            )
                        )
                    )
        key = f"{self.invocation}:messages:{len(self.captured)}"
        self.watermark = await self.store.commit_turn(
            self.conversation_id, self.watermark, key, tuple(units), job=self.job
        )
        self.captured = copy.deepcopy(messages)

    async def profile(self):
        if self.job.model_policy_id is None:
            return {"contextWindow": 32768, "maxTokens": 128}, "local-replay-v1"
        from supplyagent.service.gateway.gateway import (
            ModelCapability,
            PostgresModelAccounting,
            policy_hash,
        )

        scope = ModelCapability(
            self.identity.actor_id,
            self.identity.organization_id,
            self.job.trace_run_id,
            self.job.model_policy_id,
            0,
        )
        policy = await PostgresModelAccounting(self.store.tenants).policy(scope)
        return {
            "contextWindow": policy.context_window,
            "maxTokens": policy.max_output_tokens,
        }, policy_hash(policy)

    async def prepare(self, header, body):
        if not isinstance(body, dict) or set(body) != {"messages"}:
            raise ContextError("context_invalid")
        self.ordinal += 1
        key = f"{self.invocation}-{self.ordinal}"
        if header["modelCallKey"] != key:
            raise ContextError("context_call_key_invalid")
        await self.capture(body["messages"])
        if self.tools.runtime.store.durable:
            await self.tools.runtime.store.check_delivery(tuple(self.outcomes.values()))
        messages = copy.deepcopy(body["messages"])
        for message in messages:
            if message["role"] == "toolResult" and message["toolCallId"] in self.outcomes:
                message["content"] = [
                    {
                        "type": "text",
                        "text": canonical(self.outcomes[message["toolCallId"]].payload),
                    }
                ]
        epoch_before = await self.store.epoch(self.conversation_id)
        profile, policy_hash = await self.profile()
        projection = await self.compiler.prepare(
            messages,
            self.context.instructions or "Use only the registered supply tool.",
            self.tools.binding.project(wire_names=self.job.model_policy_id is not None),
            profile,
            working=self.working,
            scope=self.memory_scope,
        )
        identifier = uuid4()

        async def operation(c):
            current_epoch = await (
                await c.execute(
                    "select source_epoch from app.context_guard(%s,%s,%s,%s)",
                    (self.conversation_id, self.job.id, self.identity.owner, self.job.generation),
                )
            ).fetchone()
            if current_epoch["source_epoch"] != epoch_before:
                raise ContextError("context_summary_invalid")
            if projection.get("compaction"):
                state = (
                    WorkingState.model_validate_json(canonical(self.working))
                    if self.working
                    else WorkingState(goal=self.context.task)
                )
                await c.execute(
                    "select app.context_compact(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        self.conversation_id,
                        self.job.id,
                        self.identity.owner,
                        self.job.generation,
                        epoch_before,
                        self.watermark,
                        Jsonb(state.model_dump(mode="json", by_alias=True)),
                        Jsonb(projection["compaction"]),
                    ),
                )
            hashed = await (
                await c.execute(
                    "select app.context_prepare(%s,%s,%s,%s,%s,%s,%s,%s,%s) as hash",
                    (
                        identifier,
                        self.conversation_id,
                        self.job.id,
                        self.identity.owner,
                        self.job.generation,
                        self.invocation,
                        key,
                        policy_hash,
                        Jsonb(projection),
                    ),
                )
            ).fetchone()
            epoch = await (
                await c.execute(
                    "select source_epoch from app.context_snapshot where id=%s", (identifier,)
                )
            ).fetchone()
            return hashed["hash"], epoch["source_epoch"]

        projection_hash, epoch = await self.store._transaction(operation)
        return (
            identifier,
            projection,
            {
                "requestId": header["requestId"],
                "snapshotId": str(identifier),
                "modelCallKey": key,
                "projectionHash": projection_hash,
                "policyHash": policy_hash,
                "epoch": epoch,
                "budget": projection["budget"],
            },
        )

    async def abandon(self):
        async def operation(c):
            await c.execute("select app.context_abandon(%s)", (self.invocation,))

        await self.store._transaction(operation)

    async def consume_local(self, event):
        if (
            self.job.model_policy_id is not None
            or set(event)
            != {
                "protocolVersion",
                "turnId",
                "seq",
                "type",
                "requestId",
                "snapshotId",
                "modelCallKey",
                "projectionHash",
            }
            or event["modelCallKey"] != f"{self.invocation}-{self.ordinal}"
        ):
            raise ContextError("context_call_key_invalid")

        async def operation(c):
            await c.execute(
                "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                (
                    event["snapshotId"],
                    self.job.trace_run_id,
                    event["modelCallKey"],
                    "local-replay-v1",
                    event["projectionHash"],
                    None,
                ),
            )

        await self.store._transaction(operation)

    async def finish(self, body):
        if not isinstance(body, dict) or set(body) != {"messages"}:
            raise ContextError("context_invalid")
        await self.capture(body["messages"])
        references = []
        for outcome in self.outcomes.values():
            if outcome.result_ref:
                references.append(EvidenceRef.model_validate_json(canonical(outcome.result_ref)))
        state = WorkingState(
            goal=self.context.task,
            accepted_evidence=tuple(references),
            unknowns=tuple(
                str(v)
                for o in self.outcomes.values()
                if isinstance(o.payload, dict)
                for v in o.payload.get("unknowns", [])
            ),
        )
        current = await self.store.rebuild(self.conversation_id, self.job)
        await self.store.checkpoint(
            self.conversation_id, current["version"], self.watermark, state, self.job
        )

        async def operation(c):
            await c.execute(
                "select app.context_finish(%s,%s,%s,%s,%s)",
                (
                    self.conversation_id,
                    self.job.id,
                    self.identity.owner,
                    self.job.generation,
                    self.watermark,
                ),
            )

        await self.store._transaction(operation)
