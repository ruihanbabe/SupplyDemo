"""F48 common process summary and durable worker; no role-specific extractors."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

from pydantic import ValidationError

from supplyagent.sdk.context.compiler import canonical
from supplyagent.service.memory.mem0_adapter import MemoryBackendError, MemoryScope, admit
from supplyagent.service.persistence.context_store import MessageUnit, TranscriptMessage


async def finish_thread(operation):
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except (MemoryBackendError, OSError):
            pass
        raise


def closed_units(rows):
    """Validate stored boundaries again before treating any range as summary input."""
    if not rows or not any("message_unit_id" in row for row in rows):
        return [[row] for row in rows]
    units, seen = [], set()
    try:
        for index, row in enumerate(rows):
            if (
                not row["unit_complete"]
                or index
                and row["ordinal"] != rows[index - 1]["ordinal"] + 1
            ):
                raise ValueError("unclosed range")
            identifier = row["message_unit_id"]
            if not units or units[-1][0]["message_unit_id"] != identifier:
                if identifier in seen:
                    raise ValueError("noncontiguous unit")
                seen.add(identifier)
                units.append([])
            units[-1].append(row)
        for unit in units:
            MessageUnit(
                unit_id=UUID(unit[0]["message_unit_id"]),
                messages=tuple(
                    TranscriptMessage(
                        role=row["role"],
                        payload=row["payload"],
                        invocation_id=row.get("invocation_id"),
                        model_call_key=row.get("model_call_key"),
                        batch_id=row.get("batch_id"),
                        call_id=row.get("call_id"),
                        index=row.get("call_index"),
                    )
                    for row in unit
                ),
            )
    except (KeyError, ValueError, TypeError, ValidationError):
        raise MemoryBackendError("memory_source_unclosed") from None
    return units


def summarize(source, rows):
    if source["explicit_text"] is not None:
        admit(source["explicit_text"])
        return source["explicit_text"]
    # Classify the complete input before any summary/model/embedding call.
    closed_units(rows)
    admit(canonical([{"role": r["role"], "payload": r["payload"]} for r in rows]))
    paragraphs = []
    for row in rows:
        payload, role = row["payload"], row["role"]
        if role == "user":
            text = payload.get("text", "")
        elif role == "assistant":
            text = "\n".join(
                b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
            )
            if payload.get("toolCalls"):
                text += "\n" + canonical({"toolCalls": payload["toolCalls"]})
        else:
            text = canonical(payload)
        for paragraph in text.split("\n"):
            if paragraph:
                paragraphs.append(f"[{role}] {paragraph}")
    body = "Historical process; assistant statements remain unverified:\n" + "\n".join(paragraphs)
    admit(body)
    if len(body.encode()) > 8192:
        raise MemoryBackendError("memory_summary_too_large")
    return body


class MemoryWorker:
    def __init__(self, store, backend_factory, *, summary_endpoint=None):
        self.store, self.backend_factory = store, backend_factory
        self.summary_endpoint = summary_endpoint

    async def run_once(self, cancel_event=None):
        if cancel_event and cancel_event.is_set():
            return {"status": "cancelled"}
        await self.store.expire()
        deletion = await self.store.pending_deletion()
        if deletion:
            try:
                request = await self.store.erase(deletion["id"], self.backend_factory)
                return {"status": request["state"], "deleteRequestId": str(request["id"])}
            except MemoryBackendError as error:
                return {"status": "erase_pending", "code": error.code}
        work = await self.store.claim()
        if not work:
            return {"status": "idle"}
        try:
            source, rows = await self.store.source(work["source_id"])
            if cancel_event and cancel_event.is_set():
                raise MemoryBackendError("memory_cancelled")
            version = await self.store.accepted(work)
            if version is None:
                body = summarize(source, rows)
                if source["explicit_text"] is None and self.summary_endpoint:
                    from supplyagent.service.memory.models import LocalModelPort

                    port = LocalModelPort(
                        self.summary_endpoint,
                        self.store.meter(
                            self.store.scope(source), work, epoch=source["source_epoch"]
                        ),
                    )
                    body = await finish_thread(lambda: port.summarize(body))
                if cancel_event and cancel_event.is_set():
                    raise MemoryBackendError("memory_cancelled")
                version = await self.store.accept(work, source["source_epoch"], body)
            if version["status"] == "candidate":
                await self.store.pause(work)
                return {"status": "review_pending", "versionId": str(version["id"])}
            if version["status"] not in {"ready", "active"}:
                await self.store.pause(work, "memory_source_changed")
                return {"status": "quarantined", "code": "memory_source_changed"}
            await self.store.publish(
                work, source, version, self.backend_factory, cancel_event=cancel_event
            )
            return {"status": "indexed", "versionId": str(version["id"])}
        except asyncio.CancelledError:
            await self.store.cancel(work)
            raise
        except MemoryBackendError as error:
            if error.code == "memory_cancelled":
                await self.store.cancel(work)
                return {"status": "cancelled"}
            if error.code not in {
                "memory_lease_lost",
                "memory_store_failed",
                "memory_backend_unavailable",
                "memory_write_outcome_unknown",
                "memory_model_unavailable",
                "memory_usage_unknown",
            }:
                try:
                    await self.store.pause(work, error.code)
                except MemoryBackendError as pause_error:
                    if pause_error.code != "memory_lease_lost":
                        raise
                return {"status": "quarantined", "code": error.code}
            # Retain unknown publications for generation-fenced reconciliation after lease expiry.
            return {"status": "reconciliation_required", "code": error.code}


class MemoryService:
    def __init__(self, store, backend_factory):
        self.store, self.backend_factory = store, backend_factory

    async def recall(self, query, *, scope):
        if (
            not isinstance(scope, MemoryScope)
            or scope.organization_id != self.store.actor.organization_id
            or scope.user_id != self.store.actor.actor_id
        ):
            raise MemoryBackendError("memory_denied")
        if isinstance(query, dict):
            query = query.get("content", "")
            if isinstance(query, list):
                query = "\n".join(b.get("text", "") for b in query if b.get("type") == "text")
        if not isinstance(query, str) or not re.search(
            r"(?i)历史|之前|上次|偏好|曾经|记得|history|previous|prior|prefer|remember", query
        ):
            return {"status": "not_requested", "items": []}
        try:
            admit(query)
        except MemoryBackendError:
            return {
                "status": "unavailable",
                "items": [],
                "required": True,
                "code": "memory_query_rejected",
            }

        async def operation(c):
            await c.execute(
                "select app.memory_domain_lock(%s,%s,%s)",
                (scope.organization_id, scope.user_id, scope.object_scope),
            )
            await c.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s,0))", (scope.namespace,)
            )

            def search():
                backend = self.backend_factory(scope)
                try:
                    backend.bind_meter(meter)
                    return backend.search(query)
                finally:
                    backend.close()

            return await finish_thread(search)

        try:
            meter = self.store.meter(scope)
            hits = await self.store.transaction(operation)
            ids = [str(UUID(hit["metadata"]["publication_key"])) for hit in hits]
            versions = await self.store.hydrate(scope, ids)
        except (MemoryBackendError, OSError, ValueError, KeyError):
            return {"status": "unavailable", "items": [], "required": True}
        matches = {str(v["id"]): v for v in versions}
        hits.sort(
            key=lambda hit: (
                matches.get(hit["metadata"]["publication_key"], {}).get("temperature") == "cold"
            )
        )
        items, seen = [], set()
        for hit in hits:
            identifier = hit["metadata"]["publication_key"]
            version = matches.get(identifier)
            if (
                not version
                or identifier in seen
                or hit["memory"] != version["body"]
                or hit["metadata"].get("content_hash") != version["content_hash"]
            ):
                continue
            seen.add(identifier)

            async def evidence(c, identifier=version["id"]):
                return await (
                    await c.execute(
                        "select s.id,s.source_hash,s.conversation_id,s.start_ordinal,s.watermark from app.memory_evidence e join app.memory_source s on s.id=e.source_id where e.version_id=%s",
                        (identifier,),
                    )
                ).fetchall()

            sources = await self.store.transaction(evidence)
            item = {
                "versionId": identifier,
                "body": version["body"],
                "contentHash": version["content_hash"],
                "trust": version["trust"],
                "contentKind": version["content_kind"],
                "sources": [
                    {
                        "id": str(s["id"]),
                        "hash": s["source_hash"],
                        "conversationId": str(s["conversation_id"])
                        if s["conversation_id"]
                        else None,
                        "watermark": s["watermark"],
                        "startOrdinal": s["start_ordinal"],
                    }
                    for s in sources
                ],
            }
            if "conditions" in version:
                from supplyagent.service.memory.governance import MemoryGovernance

                item["governance"] = await MemoryGovernance(self.store).describe(version["id"])
            if len(canonical([*items, item]).encode()) > 8192:
                continue
            items.append(item)
            if len(items) == 5:
                break
        return {"status": "available", "items": items, "required": True}

    async def read(self, version_id, *, scope, level="L1"):
        if level not in {"L0", "L1", "L2"}:
            raise ValueError("invalid memory read level")
        rows = await self.store.hydrate(scope, [str(version_id)])
        if not rows:
            raise MemoryBackendError("memory_denied")
        version = rows[0]
        if level == "L0":
            return {
                "versionId": str(version["id"]),
                "trust": version["trust"],
                "bytes": len(version["body"].encode()),
            }
        if level == "L1":
            qualifiers = {}
            if "conditions" in version:
                from supplyagent.service.memory.governance import MemoryGovernance

                qualifiers["governance"] = await MemoryGovernance(self.store).describe(
                    version["id"]
                )
            return {
                "versionId": str(version["id"]),
                "trust": version["trust"],
                "body": version["body"],
                "coverage": version["summary_manifest"],
                **qualifiers,
            }

        async def evidence(c):
            return await (
                await c.execute(
                    "select source_id from app.memory_evidence where version_id=%s", (version_id,)
                )
            ).fetchall()

        sources = await self.store.transaction(evidence)
        return [await self.store.source(source["source_id"]) for source in sources]

    async def correct(self, version_id, text):
        return await self.store.correct(version_id, text)

    async def forget(self, version_id, *, mode="soft", include_source=False):
        if mode not in {"soft", "hard"} or type(include_source) is not bool:
            raise ValueError("invalid forget scope")
        request_id = await self.store.forget(version_id, mode, include_source)
        if mode == "hard":
            return await self.store.erase(request_id, self.backend_factory)
        return await self.store.deletion(request_id)
