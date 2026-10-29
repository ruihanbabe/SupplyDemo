"""F36 long-term-memory states; persistence and Trace logging stay in PostgreSQL."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID, uuid4

from psycopg import AsyncConnection


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REVOKED = "revoked"
    PENDING_REVALIDATION = "pending_revalidation"


@dataclass(frozen=True)
class LongTermMemory:
    id: UUID
    content: dict[str, object]
    source_id: UUID
    status: MemoryStatus
    source_kind: str
    confirmed_by: UUID | None = None

    @classmethod
    def model_candidate(cls, source_id: UUID, content: dict[str, object]) -> LongTermMemory:
        encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        if not content or len(encoded) > 16_000:
            raise ValueError(
                "memory content must be a non-empty JSON object of at most 16000 characters"
            )
        return cls(uuid4(), content, source_id, MemoryStatus.CANDIDATE, "model_candidate")

    @property
    def usable(self) -> bool:
        return self.status is MemoryStatus.CONFIRMED

    def confirmed(self, actor_id: UUID) -> LongTermMemory:
        if self.status is not MemoryStatus.CANDIDATE:
            raise ValueError("only candidate memory can be confirmed")
        return replace(self, status=MemoryStatus.CONFIRMED, confirmed_by=actor_id)

    def revoked(self) -> LongTermMemory:
        return replace(self, status=MemoryStatus.REVOKED)

    def pending_revalidation(self) -> LongTermMemory:
        if self.status is not MemoryStatus.CONFIRMED:
            raise ValueError("only confirmed memory can require revalidation")
        return replace(self, status=MemoryStatus.PENDING_REVALIDATION)


class PostgresLongTermMemoryRepository:
    """All state changes name the Trace Run that records the durable transition."""

    async def propose(
        self,
        connection: AsyncConnection[dict[str, object]],
        content: dict[str, object],
        source_ref: dict[str, object],
        trace_run_id: UUID,
        case_id: UUID | None = None,
        source_version_id: UUID | None = None,
    ) -> UUID:
        return await self._uuid(
            connection,
            "select app.propose_long_term_memory(%s::jsonb,%s::jsonb,%s,%s,%s) as id",
            (
                json.dumps(content, ensure_ascii=False),
                json.dumps(source_ref, ensure_ascii=False),
                trace_run_id,
                case_id,
                source_version_id,
            ),
        )

    async def record_deterministic(
        self,
        connection: AsyncConnection[dict[str, object]],
        content: dict[str, object],
        source_ref: dict[str, object],
        trace_run_id: UUID,
        case_id: UUID | None = None,
        source_version_id: UUID | None = None,
    ) -> UUID:
        return await self._uuid(
            connection,
            "select app.record_deterministic_long_term_memory(%s::jsonb,%s::jsonb,%s,%s,%s) as id",
            (
                json.dumps(content, ensure_ascii=False),
                json.dumps(source_ref, ensure_ascii=False),
                trace_run_id,
                case_id,
                source_version_id,
            ),
        )

    async def confirm(
        self,
        connection: AsyncConnection[dict[str, object]],
        memory_id: UUID,
        trace_run_id: UUID,
        case_id: UUID | None = None,
    ) -> None:
        await self._void(
            connection,
            "select app.confirm_long_term_memory(%s,%s,%s)",
            (memory_id, trace_run_id, case_id),
        )

    async def revoke(
        self,
        connection: AsyncConnection[dict[str, object]],
        memory_id: UUID,
        trace_run_id: UUID,
        case_id: UUID | None = None,
    ) -> None:
        await self._void(
            connection,
            "select app.revoke_long_term_memory(%s,%s,%s)",
            (memory_id, trace_run_id, case_id),
        )

    async def mark_pending_revalidation(
        self,
        connection: AsyncConnection[dict[str, object]],
        source_version_id: UUID,
        trace_run_id: UUID,
        case_id: UUID | None = None,
    ) -> int:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "select app.mark_memory_pending_revalidation(%s,%s,%s) as count",
                (source_version_id, trace_run_id, case_id),
            )
            row = await cursor.fetchone()
        return int(row["count"] if row else 0)

    @staticmethod
    async def _uuid(
        connection: AsyncConnection[dict[str, object]], statement: str, values: tuple[object, ...]
    ) -> UUID:
        async with connection.cursor() as cursor:
            await cursor.execute(statement, values)
            row = await cursor.fetchone()
        if row is None or row["id"] is None:
            raise RuntimeError("long-term memory command returned no ID")
        return UUID(str(row["id"]))

    @staticmethod
    async def _void(
        connection: AsyncConnection[dict[str, object]], statement: str, values: tuple[object, ...]
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(statement, values)
