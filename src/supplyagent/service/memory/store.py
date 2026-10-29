"""F48 private SQL authority. Backend results are hints until current source checks pass."""

from __future__ import annotations

from uuid import UUID

from psycopg import Error
from psycopg.types.json import Jsonb

from supplyagent.sdk.tools.runtime import ToolAbort
from supplyagent.service.memory.mem0_adapter import MemoryBackendError, MemoryScope, admit
from supplyagent.service.persistence.context_store import ContextError, ConversationStore
from supplyagent.service.persistence.tenant import TenantAccessError


class MemoryStore:
    def __init__(self, tenants, actor):
        self.tenants, self.actor = tenants, actor
        self.context = ConversationStore(tenants, actor)

    async def transaction(self, operation):
        try:
            return await self.tenants.in_organization(
                self.actor.actor_id, self.actor.organization_id, operation
            )
        except (Error, TenantAccessError) as error:
            message = str(error.__cause__ or error)
            code = next(
                (
                    c
                    for c in (
                        "memory_lease_lost",
                        "memory_source_denied",
                        "memory_source_changed",
                        "memory_denied",
                        "memory_invalid",
                        "memory_budget_exceeded",
                    )
                    if c in message
                ),
                "memory_store_failed",
            )
            raise MemoryBackendError(code) from None

    def meter(self, scope, work=None, deletion=None, *, epoch=None):
        import asyncio

        loop = asyncio.get_running_loop()

        async def command(action, *args):
            async def operation(c):
                if action == "reserve":
                    purpose, model, digest, bound = args
                    row = await (
                        await c.execute(
                            "select app.memory_reserve(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) as id",
                            (
                                scope.namespace,
                                purpose,
                                model,
                                digest,
                                bound,
                                work["id"] if work else None,
                                getattr(self.actor, "owner", None),
                                work["generation"] if work else None,
                                deletion,
                                epoch,
                            ),
                        )
                    ).fetchone()
                    return row["id"]
                identifier, usage, state = args
                await c.execute(
                    "select app.memory_settle(%s,%s,%s)", (identifier, Jsonb(usage), state)
                )

            return await self.transaction(operation)

        def sync(action, *args):
            return asyncio.run_coroutine_threadsafe(command(action, *args), loop).result(timeout=20)

        return sync

    async def accepted(self, work):
        if not work["version_id"]:
            return None

        async def operation(c):
            return await (
                await c.execute(
                    "select * from app.memory_version where id=%s", (work["version_id"],)
                )
            ).fetchone()

        row = await self.transaction(operation)
        return dict(row) if row else None

    async def enable(self, conversation_id, *, synthetic=False):
        async def operation(c):
            await c.execute("select app.memory_enable(%s,%s)", (conversation_id, synthetic))

        await self.transaction(operation)

    async def remember(self, object_scope, text):
        admit(text)

        async def operation(c):
            return (
                await (
                    await c.execute("select app.memory_remember(%s,%s) as id", (object_scope, text))
                ).fetchone()
            )["id"]

        return await self.transaction(operation)

    async def confirm(self, version_id):
        async def operation(c):
            await c.execute("select app.memory_confirm(%s)", (version_id,))

        await self.transaction(operation)

    async def claim(self):
        async def operation(c):
            return await (
                await c.execute("select * from app.memory_claim(%s)", (self.actor.owner,))
            ).fetchone()

        return await self.transaction(operation)

    async def source(self, source_id, *, _seen=frozenset()):
        async def operation(c):
            ok = await (
                await c.execute("select app.memory_source_readable(%s) as ok", (source_id,))
            ).fetchone()
            if not ok["ok"]:
                raise MemoryBackendError("memory_source_denied")
            source = await (
                await c.execute(
                    "select s.*,c.source_epoch from app.memory_source s left join app.context_conversation c on c.id=s.conversation_id where s.id=%s",
                    (source_id,),
                )
            ).fetchone()
            rows = []
            if source["conversation_id"]:
                rows = await (
                    await c.execute(
                        "select ordinal,role,payload,message_unit_id::text,unit_complete,invocation_id,model_call_key,batch_id,call_id,call_index from app.context_message where conversation_id=%s and ordinal between %s and %s order by ordinal",
                        (source["conversation_id"], source["start_ordinal"], source["watermark"]),
                    )
                ).fetchall()
                actual = await (
                    await c.execute(
                        "select encode(public.digest(convert_to(jsonb_agg(jsonb_build_object('ordinal',ordinal,'hash',content_hash) order by ordinal)::text,'UTF8'),'sha256'),'hex') as hash from app.context_message where conversation_id=%s and ordinal between %s and %s",
                        (source["conversation_id"], source["start_ordinal"], source["watermark"]),
                    )
                ).fetchone()
                if (
                    len(rows) != source["watermark"] - source["start_ordinal"] + 1
                    or actual["hash"] != source["source_hash"]
                ):
                    raise MemoryBackendError("memory_source_changed")
            return dict(source), [dict(r) for r in rows]

        source, rows = await self.transaction(operation)
        # The range/hash alone cannot validate revoked knowledge/BOM/CRM references.
        for row in rows:
            ref = row["payload"].get("resultRef") if row["role"] == "tool_result" else None
            if ref:
                try:
                    await self.context.read_evidence(source["conversation_id"], ref)
                except (ContextError, ToolAbort):
                    raise MemoryBackendError("memory_source_denied") from None
        if source_id not in _seen:

            async def ancestors(c):
                return await (
                    await c.execute("select id from app.memory_source_ancestors(%s)", (source_id,))
                ).fetchall()

            for ancestor in await self.transaction(ancestors):
                if ancestor["id"] not in _seen | {source_id}:
                    await self.source(ancestor["id"], _seen=_seen | {source_id})
        return source, rows

    async def cancel(self, work):
        async def operation(c):
            await c.execute(
                "select app.memory_cancel(%s,%s,%s)",
                (work["id"], self.actor.owner, work["generation"]),
            )

        await self.transaction(operation)

    async def expire(self):
        async def operation(c):
            return (await (await c.execute("select app.memory_expire() as n")).fetchone())["n"]

        return await self.transaction(operation)

    async def export_deletions(self):
        async def operation(c):
            return await (
                await c.execute(
                    "select id,organization_id,owner_id,version_id,mode,include_source,inventory from app.memory_delete_request where owner_id=%s or app.is_runtime_worker()",
                    (self.actor.actor_id,),
                )
            ).fetchall()

        import json

        return json.loads(json.dumps(await self.transaction(operation), default=str))

    async def restore_deletions(self, ledger):
        async def operation(c):
            return (
                await (
                    await c.execute("select app.memory_replay_deletions(%s) as n", (Jsonb(ledger),))
                ).fetchone()
            )["n"]

        return await self.transaction(operation)

    async def save_deletion_ledger(self, path):
        import hashlib
        import json
        import os
        import tempfile
        from pathlib import Path

        path = Path(path)
        body = json.dumps(await self.export_deletions(), sort_keys=True).encode()
        if len(body) > 1048576:
            raise MemoryBackendError("memory_ledger_too_large")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                os.chmod(temporary, 0o600)
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise MemoryBackendError("memory_ledger_unavailable") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"sha256": hashlib.sha256(body).hexdigest()}

    async def restore_deletion_ledger(self, path, expected_hash):
        import hashlib
        import json
        from pathlib import Path

        await self.begin_recovery()
        try:
            path = Path(path)
            if path.stat().st_size > 1048576:
                raise ValueError("oversized ledger")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected_hash:
                raise ValueError("ledger hash mismatch")
            ledger = json.loads(body)
        except (OSError, ValueError, TypeError):
            raise MemoryBackendError("memory_ledger_invalid") from None
        return await self.restore_deletions(ledger)

    async def begin_recovery(self):
        async def operation(c):
            await c.execute("select app.memory_begin_recovery()")

        await self.transaction(operation)

    async def finish_recovery(self):
        async def operation(c):
            await c.execute("select app.memory_finish_recovery()")

        await self.transaction(operation)

    def scope(self, source):
        return MemoryScope(source["organization_id"], source["owner_id"], source["object_scope"])

    async def accept(self, work, epoch, text):
        admit(text)

        async def operation(c):
            identifier = (
                await (
                    await c.execute(
                        "select app.memory_accept(%s,%s,%s,%s,%s) as id",
                        (work["id"], self.actor.owner, work["generation"], epoch, text),
                    )
                ).fetchone()
            )["id"]
            return dict(
                await (
                    await c.execute("select * from app.memory_version where id=%s", (identifier,))
                ).fetchone()
            )

        return await self.transaction(operation)

    async def pause(self, work, code=None):
        async def operation(c):
            if code:
                await c.execute(
                    "select app.memory_quarantine(%s,%s,%s,%s)",
                    (work["id"], self.actor.owner, work["generation"], code),
                )
            else:
                await c.execute(
                    "select app.memory_wait_review(%s,%s,%s)",
                    (work["id"], self.actor.owner, work["generation"]),
                )

        await self.transaction(operation)

    async def publish(self, work, source, version, backend_factory, *, cancel_event=None):
        from supplyagent.service.memory.runtime import finish_thread

        scope = self.scope(source)
        meter = self.meter(scope, work, epoch=source["source_epoch"])

        async def operation(c):
            await c.execute(
                "select app.memory_domain_lock(%s,%s,%s)",
                (scope.organization_id, scope.user_id, scope.object_scope),
            )
            await c.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))", (scope.namespace,)
            )
            await c.execute(
                "select app.memory_guard(%s,%s,%s)",
                (work["id"], self.actor.owner, work["generation"]),
            )

            def publish():
                backend = backend_factory(scope)
                try:
                    backend.bind_meter(meter)
                    return backend.publish(version["id"], version["body"])
                except (OSError, TimeoutError):
                    raise MemoryBackendError("memory_write_outcome_unknown") from None
                finally:
                    backend.close()

            if cancel_event and cancel_event.is_set():
                raise MemoryBackendError("memory_cancelled")
            result = await finish_thread(publish)
            if cancel_event and cancel_event.is_set():
                raise MemoryBackendError("memory_cancelled")
            await c.execute(
                "select app.memory_indexed(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    work["id"],
                    self.actor.owner,
                    work["generation"],
                    source["source_epoch"],
                    Jsonb(result["ids"]),
                    scope.namespace,
                    "mem0-26cdefb:2.2.1",
                    result["contentHash"],
                ),
            )
            return result

        return await self.transaction(operation)

    async def hydrate(self, scope, identifiers):
        if (
            scope.organization_id != self.actor.organization_id
            or scope.user_id != self.actor.actor_id
        ):
            raise MemoryBackendError("memory_denied")

        async def operation(c):
            return await (
                await c.execute(
                    "select v.* from app.memory_version v join app.memory_projection p on p.version_id=v.id where v.id=any(%s) and v.owner_id=%s and v.object_scope=%s and p.state='indexed' and app.memory_version_readable(v.id)",
                    ([UUID(i) for i in identifiers], scope.user_id, scope.object_scope),
                )
            ).fetchall()

        rows = await self.transaction(operation)
        result = []
        for row in rows:

            async def evidence(c, identifier=row["id"]):
                return await (
                    await c.execute(
                        "select source_id from app.memory_evidence where version_id=%s",
                        (identifier,),
                    )
                ).fetchall()

            sources = await self.transaction(evidence)
            try:
                for evidence in sources:
                    await self.source(evidence["source_id"])
            except MemoryBackendError:
                continue
            result.append(dict(row))
        return result

    async def correct(self, version_id, text):
        admit(text)

        async def operation(c):
            return (
                await (
                    await c.execute("select app.memory_correct(%s,%s) as id", (version_id, text))
                ).fetchone()
            )["id"]

        return await self.transaction(operation)

    async def forget(self, version_id, mode, include_source):
        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.memory_forget(%s,%s,%s) as id",
                        (version_id, mode, include_source),
                    )
                ).fetchone()
            )["id"]

        return await self.transaction(operation)

    async def deletion(self, request_id):
        async def operation(c):
            row = await (
                await c.execute(
                    "select d.*,v.object_scope from app.memory_delete_request d join app.memory_version v on v.id=d.version_id where d.id=%s and (d.owner_id=%s or app.is_runtime_worker())",
                    (request_id, self.actor.actor_id),
                )
            ).fetchone()
            if not row:
                raise MemoryBackendError("memory_denied")
            return dict(row)

        return await self.transaction(operation)

    async def erase(self, request_id, backend_factory):
        from supplyagent.service.memory.runtime import finish_thread

        request = await self.deletion(request_id)
        if request["mode"] != "hard" or request["state"] != "requested":
            return request
        scope = MemoryScope(
            request["organization_id"], request["owner_id"], request["object_scope"]
        )
        # Rebuild uses the deleting actor's own private domain, not an old business lease.

        async def operation(c):
            await c.execute(
                "select app.memory_domain_lock(%s,%s,%s)",
                (scope.organization_id, scope.user_id, scope.object_scope),
            )
            await c.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))", (scope.namespace,)
            )
            epoch = (
                await (await c.execute("select app.memory_current_epoch() as epoch")).fetchone()
            )["epoch"]
            # Re-read after locking: another eraser may already have completed this request.
            current = await (
                await c.execute(
                    "select state from app.memory_delete_request where id=%s",
                    (request_id,),
                )
            ).fetchone()
            if current["state"] != "requested":
                return
            candidates = await (
                await c.execute(
                    "select v.id from app.memory_version v join app.memory_projection p on p.version_id=v.id where v.owner_id=%s and v.object_scope=%s and p.state='indexed' and app.memory_version_readable(v.id)",
                    (scope.user_id, scope.object_scope),
                )
            ).fetchall()
            survivors = []
            for candidate in candidates:
                references = await (
                    await c.execute(
                        "select source_id from app.memory_evidence where version_id=%s",
                        (candidate["id"],),
                    )
                ).fetchall()
                try:
                    for reference in references:
                        await self.source(reference["source_id"])
                except MemoryBackendError:
                    continue
                row = await (
                    await c.execute(
                        "select * from app.memory_version where id=%s and app.memory_version_readable(id)",
                        (candidate["id"],),
                    )
                ).fetchone()
                if row:
                    survivors.append(dict(row))

            meter = self.meter(scope, deletion=request_id, epoch=epoch)

            def erase():
                backend = backend_factory(scope)
                try:
                    backend.bind_meter(meter)
                    return backend.erase_and_rebuild([(v["id"], v["body"]) for v in survivors])
                except (OSError, TimeoutError):
                    raise MemoryBackendError("memory_erase_incomplete") from None
                finally:
                    backend.close()

            if (await (await c.execute("select app.memory_current_epoch() as epoch")).fetchone())[
                "epoch"
            ] != epoch:
                raise MemoryBackendError("memory_source_changed")
            inventory = await finish_thread(erase)
            await c.execute(
                "select app.memory_erased(%s,%s,%s)",
                (request_id, Jsonb(inventory), Jsonb(inventory["rebuilt"])),
            )

        await self.transaction(operation)
        return await self.deletion(request_id)

    async def pending_deletion(self):
        async def operation(c):
            return await (
                await c.execute(
                    "select id from app.memory_delete_request where state='requested' and app.is_runtime_worker() order by created_at limit 1"
                )
            ).fetchone()

        return await self.transaction(operation)
