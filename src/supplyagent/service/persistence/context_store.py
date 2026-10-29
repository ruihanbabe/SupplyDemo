"""F52 C1: private source commits, complete tool units and working checkpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from supplyagent.service.persistence.tenant import TenantAccessError


class ContextError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


class TranscriptMessage(StrictModel):
    role: Literal["user", "assistant", "tool_result", "control"]
    payload: dict
    invocation_id: UUID | None = Field(default=None, alias="invocationId")
    model_call_key: str | None = Field(default=None, alias="modelCallKey", max_length=241)
    batch_id: int | None = Field(default=None, alias="batchId", ge=1)
    call_id: str | None = Field(default=None, alias="callId", min_length=1, max_length=200)
    index: int | None = Field(default=None, ge=0, le=15)


class MessageUnit(StrictModel):
    unit_id: UUID = Field(default_factory=uuid4, alias="unitId")
    complete: bool = True
    messages: tuple[TranscriptMessage, ...] = Field(min_length=1, max_length=17)

    @model_validator(mode="after")
    def paired(self):
        head = self.messages[0]
        calls = head.payload.get("toolCalls", [])
        if not isinstance(calls, list):
            raise ValueError("invalid tool unit")  # noqa: TRY004 - Pydantic wraps ValueError only.
        if calls:
            if (
                head.role != "assistant"
                or not self.complete
                or len(calls) > 16
                or len(self.messages) != len(calls) + 1
                or head.invocation_id is None
                or head.batch_id is None
            ):
                raise ValueError("incomplete tool unit")
            seen = set()
            for i, (call, result) in enumerate(zip(calls, self.messages[1:])):
                if (
                    not isinstance(call, dict)
                    or set(call) != {"id", "name", "arguments"}
                    or not isinstance(call["id"], str)
                    or not 1 <= len(call["id"]) <= 200
                    or call["id"] in seen
                    or not isinstance(call["name"], str)
                    or not 1 <= len(call["name"]) <= 96
                ):
                    raise ValueError("invalid call")
                seen.add(call["id"])
                if result.payload.get("status") == "ok" and not isinstance(call["arguments"], dict):
                    raise ValueError("successful call requires object arguments")
                if (
                    set(result.payload) != {"status", "code", "resultRef"}
                    or result.role != "tool_result"
                    or result.call_id != call["id"]
                    or result.index != i
                    or result.invocation_id != head.invocation_id
                    or result.batch_id != head.batch_id
                    or result.model_call_key != head.model_call_key
                ):
                    raise ValueError("unpaired tool result")
        elif (
            len(self.messages) != 1
            or head.role == "tool_result"
            or not self.complete
            and head.role != "control"
        ):
            raise ValueError("invalid message unit")
        # Bound the private command, without confusing transport bytes with token budget.
        if len(self.model_dump_json(by_alias=True).encode()) > 1048576:
            raise ValueError("message unit too large")
        return self

    @classmethod
    def user(cls, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty user message")
        return cls(messages=(TranscriptMessage(role="user", payload={"text": text}),))


class EvidenceRef(StrictModel):
    source_kind: Literal["trace_artifact", "intelligence_observation"] = Field(alias="sourceKind")
    source_id: UUID = Field(alias="sourceId")
    version: str = Field(min_length=1, max_length=80)
    locator: Literal["redacted_payload", "payload"]
    fragment_hash: str = Field(alias="fragmentHash", pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def matching_locator(self):
        expected = "redacted_payload" if self.source_kind == "trace_artifact" else "payload"
        if self.locator != expected:
            raise ValueError("evidence kind and locator differ")
        return self


class WorkingStep(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    status: Literal["pending", "running", "completed", "blocked", "failed"]


class WorkingState(StrictModel):
    goal: str = Field(min_length=1, max_length=1000)
    constraints: tuple[str, ...] = ()
    steps: tuple[WorkingStep, ...] = ()
    accepted_evidence: tuple[EvidenceRef, ...] = Field(default=(), alias="acceptedEvidence")
    unknowns: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    next_step: str | None = Field(default=None, alias="nextStep", max_length=1000)

    @model_validator(mode="after")
    def bounded(self):
        if (
            len({s.id for s in self.steps}) != len(self.steps)
            or len(self.model_dump_json().encode()) > 65536
        ):
            raise ValueError("invalid working state")
        return self


@dataclass(frozen=True)
class ContextActor:
    organization_id: UUID
    actor_id: UUID


class ConversationStore:
    def __init__(self, tenants, actor):
        self.tenants, self.actor = tenants, actor

    async def _transaction(self, operation):
        from psycopg import Error

        try:
            return await self.tenants.in_organization(
                self.actor.actor_id, self.actor.organization_id, operation
            )
        except (Error, TenantAccessError) as error:
            message = str(error.__cause__ or error)
            code = next(
                (
                    code
                    for code in (
                        "lease_lost",
                        "permission_revoked",
                        "binding_invalid",
                        "context_summary_invalid",
                        "context_snapshot_invalid",
                        "context_denied",
                        "context_scope_changed",
                        "context_idempotency_conflict",
                        "context_watermark_conflict",
                        "context_version_conflict",
                        "context_closed",
                        "context_duplicate_unit",
                        "context_unpaired",
                        "context_invalid_evidence",
                        "context_invalid",
                    )
                    if code in message
                ),
                "context_denied"
                if isinstance(error, TenantAccessError)
                else "context_store_failed",
            )
            raise ContextError(code) from None

    async def open(self, job_id, role):
        async def operation(c):
            return (
                await (
                    await c.execute("select app.context_open(%s,%s) as id", (job_id, role))
                ).fetchone()
            )["id"]

        return await self._transaction(operation)

    async def commit_turn(self, conversation_id, expected_watermark, key, units, *, job=None):
        from psycopg.types.json import Jsonb

        if not 1 <= len(units) <= 128 or any(not isinstance(u, MessageUnit) for u in units):
            raise ValueError("invalid units")
        serialized = [u.model_dump(mode="json", by_alias=True) for u in units]

        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.context_commit(%s,%s,%s,%s,%s,%s,%s) as watermark",
                        (
                            conversation_id,
                            job.id if job else None,
                            getattr(self.actor, "owner", None),
                            job.generation if job else None,
                            expected_watermark,
                            key,
                            Jsonb(serialized),
                        ),
                    )
                ).fetchone()
            )["watermark"]

        return await self._transaction(operation)

    async def close(self, conversation_id, expected_watermark):
        async def operation(c):
            await c.execute(
                "select app.context_close(%s,%s)", (conversation_id, expected_watermark)
            )

        await self._transaction(operation)

    async def read_range(self, conversation_id, after=0, limit=128):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("invalid context page")

        async def operation(c):
            accessible = await (
                await c.execute("select app.context_can_read(%s) as allowed", (conversation_id,))
            ).fetchone()
            if not accessible["allowed"]:
                raise ContextError("context_denied")
            # Page whole units, even when a byte/token consumer later applies a smaller cap.
            return await (
                await c.execute(
                    """select * from app.context_message where conversation_id=%s and message_unit_id in
                (select message_unit_id from app.context_message where conversation_id=%s and ordinal>%s group by message_unit_id order by min(ordinal) limit %s)
                order by ordinal""",
                    (conversation_id, conversation_id, after, limit),
                )
            ).fetchall()

        return await self._transaction(operation)

    async def checkpoint(self, conversation_id, expected_version, watermark, state, job):
        from psycopg.types.json import Jsonb

        if not isinstance(state, WorkingState):
            raise TypeError("invalid working state")

        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.context_checkpoint(%s,%s,%s,%s,%s,%s,%s) as version",
                        (
                            conversation_id,
                            job.id,
                            self.actor.owner,
                            job.generation,
                            expected_version,
                            watermark,
                            Jsonb(state.model_dump(mode="json", by_alias=True)),
                        ),
                    )
                ).fetchone()
            )["version"]

        return await self._transaction(operation)

    async def rebuild(self, conversation_id, job):
        from supplyagent.sdk.tools.runtime import PostgresExecutionStore

        execution = PostgresExecutionStore(self.tenants, self.actor, job)

        async def operation(c):
            conversation = await (
                await c.execute(
                    "select * from app.context_guard(%s,%s,%s,%s)",
                    (conversation_id, job.id, self.actor.owner, job.generation),
                )
            ).fetchone()
            row = await (
                await c.execute(
                    "select * from app.context_working_state where conversation_id=%s order by version desc limit 1",
                    (conversation_id,),
                )
            ).fetchone()
            state = WorkingState.model_validate_json(json.dumps(row["payload"])) if row else None
            if state:
                for ref in state.accepted_evidence:
                    if ref.source_kind == "trace_artifact":
                        source = await (
                            await c.execute(
                                """select a.redacted_payload as payload,a.content_hash from app.trace_artifact a
                                join app.runtime_run r on r.trace_run_id=a.run_id join app.runtime_job j on j.run_id=r.id
                                where a.id=%s and j.id=%s and a.organization_id=%s""",
                                (ref.source_id, row["job_id"], self.actor.organization_id),
                            )
                        ).fetchone()
                    else:
                        source = await (
                            await c.execute(
                                """select o.payload,o.content_hash from app.intelligence_observation o
                                join app.intelligence_attempt a on a.id=o.attempt_id
                                where o.id=%s and a.job_id=%s and o.organization_id=%s""",
                                (ref.source_id, row["job_id"], self.actor.organization_id),
                            )
                        ).fetchone()
                    if not source or source["content_hash"] != ref.fragment_hash:
                        raise ContextError("context_invalid_evidence")
                    await execution._validate_sources(
                        c, {"requested_by": conversation["owner_id"]}, source["payload"]
                    )
            return {
                "conversation": dict(conversation),
                "state": state,
                "version": row["version"] if row else 0,
                "resumeMode": "new_invocation",
                "memoryStatus": "disabled",
            }

        return await self._transaction(operation)

    async def epoch(self, conversation_id):
        async def operation(c):
            if not (
                await (
                    await c.execute("select app.context_can_read(%s) as ok", (conversation_id,))
                ).fetchone()
            )["ok"]:
                raise ContextError("context_denied")
            return (
                await (
                    await c.execute(
                        "select source_epoch from app.context_conversation where id=%s",
                        (conversation_id,),
                    )
                ).fetchone()
            )["source_epoch"]

        return await self._transaction(operation)

    async def read_compaction(self, conversation_id):
        async def operation(c):
            if not (
                await (
                    await c.execute("select app.context_can_read(%s) as ok", (conversation_id,))
                ).fetchone()
            )["ok"]:
                raise ContextError("context_denied")
            row = await (
                await c.execute(
                    "select w.compaction,c.source_epoch from app.context_working_state w join app.context_conversation c on c.id=w.conversation_id where c.id=%s and w.kind='compaction' order by w.version desc limit 1",
                    (conversation_id,),
                )
            ).fetchone()
            if not row:
                return {"status": "absent"}
            if row["compaction"]["sourceEpoch"] != row["source_epoch"]:
                raise ContextError("context_summary_invalid")
            return {"status": "available", "compaction": row["compaction"]}

        return await self._transaction(operation)

    async def read_evidence(self, conversation_id, raw_ref):
        from types import SimpleNamespace

        from psycopg.types.json import Jsonb

        from supplyagent.sdk.tools.runtime import PostgresExecutionStore

        ref = EvidenceRef.model_validate_json(json.dumps(raw_ref))

        async def operation(c):
            if not (
                await (
                    await c.execute("select app.context_can_read(%s) as ok", (conversation_id,))
                ).fetchone()
            )["ok"]:
                raise ContextError("context_denied")
            accepted = await (
                await c.execute(
                    "select c.owner_id, (m.context_ref->>'opportunityId')::uuid as opportunity_id from app.context_conversation c join app.context_message msg on msg.conversation_id=c.id join app.runtime_job j on j.id=msg.job_id join app.runtime_run r on r.id=j.run_id join app.runtime_mission m on m.id=r.mission_id where c.id=%s and msg.role='tool_result' and msg.payload->'resultRef'=%s limit 1",
                    (conversation_id, Jsonb(raw_ref)),
                )
            ).fetchone()
            if not accepted:
                raise ContextError("context_invalid_evidence")
            table, column = (
                ("trace_artifact", "redacted_payload")
                if ref.source_kind == "trace_artifact"
                else ("intelligence_observation", "payload")
            )
            source = await (
                await c.execute(
                    f"select {column} as payload,content_hash from app.{table} where id=%s and organization_id=%s",
                    (ref.source_id, self.actor.organization_id),
                )
            ).fetchone()
            if not source or source["content_hash"] != ref.fragment_hash:
                raise ContextError("context_invalid_evidence")
            execution = PostgresExecutionStore(
                self.tenants, self.actor, SimpleNamespace(opportunity_id=accepted["opportunity_id"])
            )
            await execution._validate_sources(
                c, {"requested_by": accepted["owner_id"]}, source["payload"]
            )
            return source["payload"]

        return await self._transaction(operation)
