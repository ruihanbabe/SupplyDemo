"""F40 的产品 Trace 领域适配器。

本模块只处理已经脱敏的审计事实。它不启动 Agent、不调用模型，也不把
Redis、浏览器或本地文件当作 Run 的权威记录；每次调用都由 F31 已建立的
同一 PostgreSQL/RLS 事务提供 connection。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection


class TraceAccessError(Exception):
    """Trace 写入或读取跨越了当前组织、Run 或 Artifact 的边界。"""


@dataclass(frozen=True)
class TraceRun:
    """一个可审计工作单元；它的事件序列只允许数据库函数推进。"""

    id: UUID
    case_id: UUID | None
    run_kind: str
    actor_id: UUID
    created_at: datetime
    next_event_sequence: int
    last_event_hash: str | None


@dataclass(frozen=True)
class TraceArtifact:
    """某个稳定 artifact_key 的一个不可变、脱敏版本。"""

    id: UUID
    artifact_key: UUID
    version: int
    artifact_type: str
    redacted_summary: str
    redacted_payload: dict[str, object]
    content_hash: str
    created_at: datetime


@dataclass(frozen=True)
class TraceEvent:
    """哈希链上的一个只追加事件；sequence_no 是 Run 内稳定顺序。"""

    id: UUID
    run_id: UUID
    sequence_no: int
    event_type: str
    redacted_summary: str
    metadata: dict[str, object]
    artifact_id: UUID | None
    prev_hash: str | None
    event_hash: str
    occurred_at: datetime


class TraceRepository(Protocol):
    """F40 的 SQL 接口；禁止自行打开连接来绕过 TenantRepository。"""

    async def begin_run(
        self, connection: AsyncConnection[dict[str, object]], run_kind: str, case_id: UUID | None
    ) -> UUID: ...

    async def append_artifact(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        case_id: UUID | None,
        artifact_key: UUID,
        artifact_type: str,
        redacted_summary: str,
        redacted_payload: dict[str, object],
        content_hash: str,
    ) -> UUID: ...

    async def append_event(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        case_id: UUID | None,
        event_type: str,
        redacted_summary: str,
        metadata: dict[str, object],
        artifact_id: UUID | None,
    ) -> UUID: ...

    async def get_run(
        self, connection: AsyncConnection[dict[str, object]], run_id: UUID
    ) -> TraceRun | None: ...

    async def list_events(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        after_event_id: UUID | None,
    ) -> list[TraceEvent]: ...

    async def verify_chain(
        self, connection: AsyncConnection[dict[str, object]], run_id: UUID
    ) -> bool: ...


class UnavailableTraceRepository:
    """数据库未装配时不返回伪造 Trace，也不接收写入。"""

    async def begin_run(self, connection: AsyncConnection[dict[str, object]], *_: object) -> UUID:
        del connection
        raise TraceAccessError("Trace 服务未配置")

    async def append_artifact(
        self, connection: AsyncConnection[dict[str, object]], *_: object
    ) -> UUID:
        del connection
        raise TraceAccessError("Trace 服务未配置")

    async def append_event(
        self, connection: AsyncConnection[dict[str, object]], *_: object
    ) -> UUID:
        del connection
        raise TraceAccessError("Trace 服务未配置")

    async def get_run(
        self, connection: AsyncConnection[dict[str, object]], run_id: UUID
    ) -> TraceRun | None:
        del connection, run_id
        raise TraceAccessError("Trace 服务未配置")

    async def list_events(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        after_event_id: UUID | None,
    ) -> list[TraceEvent]:
        del connection, run_id, after_event_id
        raise TraceAccessError("Trace 服务未配置")

    async def verify_chain(
        self, connection: AsyncConnection[dict[str, object]], run_id: UUID
    ) -> bool:
        del connection, run_id
        raise TraceAccessError("Trace 服务未配置")


class PostgresTraceRepository:
    """只调用冻结的 F40 SQL 函数，并将 RLS 行转成领域对象。"""

    async def begin_run(
        self, connection: AsyncConnection[dict[str, object]], run_kind: str, case_id: UUID | None
    ) -> UUID:
        return await self._command(
            connection, "select app.begin_trace_run(%s, %s) as id", (run_kind, case_id)
        )

    async def append_artifact(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        case_id: UUID | None,
        artifact_key: UUID,
        artifact_type: str,
        redacted_summary: str,
        redacted_payload: dict[str, object],
        content_hash: str,
    ) -> UUID:
        return await self._command(
            connection,
            "select app.append_trace_artifact(%s, %s, %s, %s, %s, %s::jsonb, %s) as id",
            (
                run_id,
                case_id,
                artifact_key,
                artifact_type,
                redacted_summary,
                canonical_json(redacted_payload),
                content_hash,
            ),
        )

    async def append_event(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        case_id: UUID | None,
        event_type: str,
        redacted_summary: str,
        metadata: dict[str, object],
        artifact_id: UUID | None,
    ) -> UUID:
        return await self._command(
            connection,
            "select app.append_trace_event(%s, %s, %s, %s, %s::jsonb, %s) as id",
            (run_id, case_id, event_type, redacted_summary, canonical_json(metadata), artifact_id),
        )

    async def get_run(
        self, connection: AsyncConnection[dict[str, object]], run_id: UUID
    ) -> TraceRun | None:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """select id, case_id, run_kind, actor_id, created_at, next_event_sequence, last_event_hash
                   from app.trace_run where id = %s""",
                (run_id,),
            )
            row = await cursor.fetchone()
        return self._run(row) if row else None

    async def list_events(
        self,
        connection: AsyncConnection[dict[str, object]],
        run_id: UUID,
        after_event_id: UUID | None,
    ) -> list[TraceEvent]:
        # 先把 Last-Event-ID 映射为本 Run 内序号；外组织或其他 Run 的 ID 不可作为游标。
        after_sequence = 0
        if after_event_id is not None:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select sequence_no from app.trace_event where id = %s and run_id = %s",
                    (after_event_id, run_id),
                )
                row = await cursor.fetchone()
            if row is None:
                raise TraceAccessError("事件游标不属于当前组织或 Run")
            after_sequence = int(row["sequence_no"])
        async with connection.cursor() as cursor:
            await cursor.execute(
                """select id, run_id, sequence_no, event_type, redacted_summary, metadata, artifact_id,
                          prev_hash, event_hash, occurred_at
                   from app.trace_event where run_id = %s and sequence_no > %s order by sequence_no""",
                (run_id, after_sequence),
            )
            rows = await cursor.fetchall()
        return [self._event(row) for row in rows]

    async def verify_chain(
        self, connection: AsyncConnection[dict[str, object]], run_id: UUID
    ) -> bool:
        async with connection.cursor() as cursor:
            await cursor.execute("select app.verify_trace_chain(%s) as valid", (run_id,))
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Trace 哈希校验未返回结果")
        return bool(row["valid"])

    async def _command(
        self, connection: AsyncConnection[dict[str, object]], query: str, values: tuple[object, ...]
    ) -> UUID:
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(query, values)
                row = await cursor.fetchone()
        except Exception as error:
            if getattr(error, "sqlstate", None) == "42501":
                raise TraceAccessError("无权访问当前 Trace 上下文") from error
            raise
        if row is None:
            raise RuntimeError("Trace 写入未返回标识")
        return UUID(str(row["id"]))

    @staticmethod
    def _run(row: dict[str, object]) -> TraceRun:
        return TraceRun(
            UUID(str(row["id"])),
            UUID(str(row["case_id"])) if row["case_id"] else None,
            str(row["run_kind"]),
            UUID(str(row["actor_id"])),
            row["created_at"],
            int(row["next_event_sequence"]),
            str(row["last_event_hash"]) if row["last_event_hash"] else None,
        )

    @staticmethod
    def _event(row: dict[str, object]) -> TraceEvent:
        metadata = row["metadata"]
        if not isinstance(metadata, dict):
            raise TypeError("trace_event.metadata 必须是对象")
        return TraceEvent(
            UUID(str(row["id"])),
            UUID(str(row["run_id"])),
            int(row["sequence_no"]),
            str(row["event_type"]),
            str(row["redacted_summary"]),
            metadata,
            UUID(str(row["artifact_id"])) if row["artifact_id"] else None,
            str(row["prev_hash"]) if row["prev_hash"] else None,
            str(row["event_hash"]),
            row["occurred_at"],
        )


def canonical_json(value: dict[str, object]) -> str:
    """统一 Artifact 哈希与数据库 JSON 输入的字段顺序，避免等值对象出现不同摘要。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def content_hash(redacted_payload: dict[str, object]) -> str:
    """内容摘要由 Python 重算；调用者不能伪造 Artifact 的完整性声明。"""
    return hashlib.sha256(canonical_json(redacted_payload).encode("utf-8")).hexdigest()
