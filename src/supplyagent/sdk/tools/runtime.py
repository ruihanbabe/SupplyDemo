"""F51 execution boundary; domain adapters keep their original result stores."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from supplyagent.sdk.tools.registry import ToolInputError, ToolUnavailableError

CURRENT_TOOL_CALL: ContextVar[dict | None] = ContextVar("current_tool_call", default=None)
_ACCEPTING: ContextVar[Any] = ContextVar("accepting_tool", default=None)


class ToolRuntimeError(Exception):
    """Compatibility error for v1 callers."""


class ToolAbort(ToolRuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CallIdentity:
    invocation_id: UUID
    call_id: str
    batch_id: int = 1
    index: int = 0
    model_call_key: str | None = None

    def metadata(self):
        return {
            "invocationId": str(self.invocation_id),
            "toolCallId": self.call_id,
            "modelCallKey": self.model_call_key,
            "batchId": self.batch_id,
            "index": self.index,
        }


@dataclass(frozen=True)
class ToolOutcome:
    status: str
    code: str | None
    payload: object
    result_ref: dict | None = None

    def wire(self):
        return {
            "status": self.status,
            "code": self.code,
            "modelPayload": self.payload,
            "resultRef": self.result_ref,
        }


@dataclass(frozen=True)
class PreparedCall:
    identity: CallIdentity
    name: str
    arguments_json: str
    definition: Any = field(repr=False, compare=False)
    spec: Any = field(repr=False, compare=False)
    ticket: Any = field(repr=False, compare=False)
    owner: object = field(repr=False, compare=False)


class MemoryExecutionStore:
    """Explicit non-durable store for standalone/fixture engines, never SQL recovery."""

    durable = False

    def __init__(self):
        self.rows = {}

    async def begin(self, identity, requested_name, arguments, spec, binding, error, policy=None):
        key = (identity.invocation_id, identity.call_id)
        if key in self.rows:
            raise ToolAbort("duplicate_call")
        ticket = {"identity": identity, "state": "rejected" if error else "prepared"}
        self.rows[key] = ticket
        return ticket

    async def check(self, ticket):
        return None

    async def running(self, ticket):
        ticket["state"] = "running"

    async def finish(self, ticket, state, code=None):
        ticket["state"] = state


class ToolRuntime:
    def __init__(self, definitions, binding):
        self.definitions = {item.name: item for item in definitions}
        self.binding = binding
        stores = [item.execution_store for item in definitions if item.execution_store is not None]
        if stores and (len(stores) != len(definitions) or any(s is not stores[0] for s in stores)):
            raise ValueError("one invocation must use one execution store")
        self.store = stores[0] if stores else MemoryExecutionStore()
        self._owner = object()
        self._consumed = set()

    async def prepare(self, identity, name, arguments, policy=None):
        spec, definition, error = None, None, None
        try:
            spec = self.binding.resolve(name)
            definition = self.definitions[spec.name]
            spec.validate(arguments)
            if definition.validator is not None:
                definition.validator(arguments)
        except ToolUnavailableError:
            error = "unknown_tool"
        except (ToolInputError, ToolRuntimeError, TypeError, ValueError):
            error = "invalid_arguments"
        ticket = await self.store.begin(
            identity, name, arguments, spec, self.binding, error, policy=policy
        )
        if error:
            return ToolOutcome("error", error, {"error": error})
        return PreparedCall(
            identity,
            spec.name,
            json.dumps(arguments, sort_keys=True),
            definition,
            spec,
            ticket,
            self._owner,
        )

    async def execute(self, prepared, cancel=None):
        key = (prepared.identity.invocation_id, prepared.identity.call_id)
        if prepared.owner is not self._owner or key in self._consumed:
            raise ToolAbort("duplicate_call")
        self._consumed.add(key)
        if cancel is not None and cancel.is_set():
            await self.store.finish(prepared.ticket, "cancelled", "cancelled")
            raise ToolAbort("cancelled")
        await self.store.check(prepared.ticket)
        await self.store.running(prepared.ticket)
        call_token = CURRENT_TOOL_CALL.set(prepared.identity.metadata())
        session = {"store": self.store, "ticket": prepared.ticket, "accepted": False}
        session_token = _ACCEPTING.set(session)
        try:
            result = prepared.definition.handler(json.loads(prepared.arguments_json))
            result = await result if inspect.isawaitable(result) else result
            if cancel is not None and cancel.is_set():
                raise ToolAbort("cancelled")
            await self.store.check(prepared.ticket)
            if self.store.durable and (
                not session["accepted"]
                or session.get("accepted_payload")
                != json.dumps(result, sort_keys=True, default=str)
            ):
                raise ToolAbort("persistence_failed")
            if not self.store.durable:
                await self.store.finish(prepared.ticket, "succeeded")
            return ToolOutcome(
                "ok",
                None,
                copy.deepcopy(result),
                copy.deepcopy(result.get("citation")) if isinstance(result, dict) else None,
            )
        except asyncio.CancelledError:
            await self.store.finish(prepared.ticket, "cancelled", "cancelled")
            raise
        except ToolAbort as error:
            await self.store.finish(prepared.ticket, "failed", error.code)
            raise
        except (ConnectionError, TimeoutError):
            await self.store.finish(prepared.ticket, "failed", "service_unavailable")
            return ToolOutcome("error", "service_unavailable", {"error": "service_unavailable"})
        except Exception as error:
            name = type(error).__name__
            cause = str(error.__cause__ or error)
            known = (
                next(
                    (
                        code
                        for code in ("lease_lost", "binding_invalid", "permission_revoked")
                        if code in cause
                    ),
                    None,
                )
                if getattr(error.__cause__, "sqlstate", None)
                else None
            )
            if known:
                code = known
            elif "invalid result reference" in cause:
                code = "persistence_failed"
            else:
                code = (
                    "permission_revoked"
                    if name in {"TenantAccessError", "CapabilityAccessError", "CrmFactAccessError"}
                    else "lease_lost"
                    if name == "RuntimeStateError"
                    else "binding_invalid"
                    if isinstance(error, ToolRuntimeError)
                    else "persistence_failed"
                    if hasattr(error, "sqlstate")
                    else "internal_error"
                )
            await self.store.finish(prepared.ticket, "failed", code)
            raise ToolAbort(code) from error
        finally:
            _ACCEPTING.reset(session_token)
            CURRENT_TOOL_CALL.reset(call_token)


async def accept_tool_result(connection, payload):
    """Called inside the existing domain result transaction, before its commit."""
    session = _ACCEPTING.get()
    if session is not None and session["store"].durable:
        await session["store"].accept(connection, session["ticket"], payload)
        session["accepted"] = True
        session["accepted_payload"] = json.dumps(payload, sort_keys=True, default=str)
    return payload


async def tool_reader_context(connection):
    session = _ACCEPTING.get()
    if session is not None and session["store"].durable:
        await connection.execute(
            "select app.set_request_context(%s,%s)",
            (session["ticket"]["requested_by"], session["store"].identity.organization_id),
        )


async def tool_worker_context(connection):
    session = _ACCEPTING.get()
    if session is not None and session["store"].durable:
        identity = session["store"].identity
        await connection.execute(
            "select app.set_request_context(%s,%s)", (identity.actor_id, identity.organization_id)
        )


class PostgresExecutionStore:
    durable = True

    def __init__(self, tenants, identity, job):
        self.tenants, self.identity, self.job = tenants, identity, job

    async def _transaction(self, operation):
        from psycopg import Error

        from supplyagent.service.persistence.tenant import TenantAccessError

        try:
            return await self.tenants.in_organization(
                self.identity.actor_id, self.identity.organization_id, operation
            )
        except TenantAccessError as error:
            cause = str(error.__cause__ or error)
            code = next(
                (c for c in ("lease_lost", "binding_invalid", "permission_revoked") if c in cause),
                "permission_revoked",
            )
            raise ToolAbort(code) from None
        except Error as error:
            text = str(error)
            code = next(
                (
                    c
                    for c in (
                        "lease_lost",
                        "binding_invalid",
                        "permission_revoked",
                        "duplicate_call",
                    )
                    if c in text
                ),
                "persistence_failed",
            )
            raise ToolAbort(code) from None

    async def begin(self, identity, name, arguments, spec, binding, error, policy=None):
        from psycopg.types.json import Jsonb

        metadata = {
            "invocationId": str(identity.invocation_id),
            "callId": identity.call_id,
            "batchId": identity.batch_id,
            "index": identity.index,
            "modelCallKey": identity.model_call_key,
            "requestedName": name,
            "canonicalName": spec.name if spec else None,
            "specVersion": spec.version if spec else None,
            "bindingHash": binding.snapshot["bindingHash"],
            "scheduling": {
                **(spec.scheduling if spec else {}),
                "policy": policy or {"schemaVersion": 1, "strategy": "serial"},
            },
            "error": error,
        }

        async def operation(c):
            row = await (
                await c.execute(
                    "select * from app.tool_begin(%s,%s,%s,%s,%s)",
                    (
                        self.job.id,
                        self.identity.owner,
                        self.job.generation,
                        Jsonb(metadata),
                        Jsonb(arguments),
                    ),
                )
            ).fetchone()
            return dict(row)

        ticket = await self._transaction(operation)
        self._requested_by = ticket["requested_by"]
        return ticket

    async def check(self, ticket):
        async def operation(c):
            await c.execute(
                "select app.tool_guard(%s,%s,%s)",
                (self.job.id, self.identity.owner, self.job.generation),
            )

        await self._transaction(operation)

    async def running(self, ticket):
        await self.finish(ticket, "running")

    async def finish(self, ticket, state, code=None):
        async def operation(c):
            await c.execute(
                "select app.tool_finish(%s,%s,%s,%s,null)",
                (ticket["execution_id"], self.identity.owner, state, code),
            )

        await self._transaction(operation)

    async def _validate_sources(self, connection, ticket, payload):
        sources = list(payload.get("sourceCitations", ()))
        sources.extend(hit["citation"] for hit in payload.get("hits", ()) if "citation" in hit)
        if sources:
            from supplyagent.service.domains.commercial import citation_json, source_is_current
            from supplyagent.service.domains.crm import PostgresCrmFactRepository
            from supplyagent.service.domains.knowledge import PostgresKnowledgeRepository
            from supplyagent.service.workflows.specialists import Citation

            await connection.execute(
                "select app.set_request_context(%s,%s)",
                (ticket["requested_by"], self.identity.organization_id),
            )
            crm_refs = None
            for source in sources:
                kind = source["sourceKind"]
                valid = True
                if kind == "crm_record":
                    if crm_refs is None:
                        facts = await PostgresCrmFactRepository().load_opportunity_facts(
                            connection, self.job.opportunity_id
                        )
                        crm_refs = [citation_json(r.citation()) for r in facts.references()]
                    valid = source in crm_refs
                elif kind in {"bom_revision", "quote_revision"}:
                    valid = await source_is_current(connection, source)
                elif kind == "knowledge_chunk":
                    valid = await PostgresKnowledgeRepository().citation_is_valid(
                        connection,
                        Citation(
                            source["sourceKind"],
                            source["sourceId"],
                            source["version"],
                            source["locator"],
                            source["fragmentHash"],
                        ),
                    )
                if not valid:
                    raise ToolAbort("permission_revoked")
        await connection.execute(
            "select app.set_request_context(%s,%s)",
            (self.identity.actor_id, self.identity.organization_id),
        )

    async def accept(self, connection, ticket, payload):
        from psycopg.types.json import Jsonb

        await self._validate_sources(connection, ticket, payload)
        await tool_worker_context(connection)
        await connection.execute(
            "select app.tool_finish(%s,%s,'succeeded',null,%s)",
            (ticket["execution_id"], self.identity.owner, Jsonb(payload)),
        )

    async def check_delivery(self, outcomes):
        async def operation(c):
            await c.execute(
                "select app.tool_guard(%s,%s,%s)",
                (self.job.id, self.identity.owner, self.job.generation),
            )
            for outcome in outcomes:
                if outcome.status == "ok":
                    await self._validate_sources(
                        c, {"requested_by": self._requested_by}, outcome.payload
                    )

        await self._transaction(operation)

    async def acquire(self, ticket, token, resources, seconds):
        from psycopg.types.json import Jsonb

        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.tool_resource_acquire(%s,%s,%s,%s,%s) as ok",
                        (
                            ticket["execution_id"],
                            self.identity.owner,
                            token,
                            Jsonb(resources),
                            seconds,
                        ),
                    )
                ).fetchone()
            )["ok"]

        return await self._transaction(operation)

    async def release(self, ticket, token, clean):
        async def operation(c):
            await c.execute(
                "select app.tool_resource_release(%s,%s,%s)", (ticket["execution_id"], token, clean)
            )

        await self._transaction(operation)

    async def renew(self, ticket, token, seconds):
        async def operation(c):
            await c.execute(
                "select app.tool_resource_renew(%s,%s,%s,%s)",
                (ticket["execution_id"], self.identity.owner, token, seconds),
            )

        await self._transaction(operation)
