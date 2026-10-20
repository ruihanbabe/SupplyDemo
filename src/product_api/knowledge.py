"""F36 组织知识 Repository；连接和租户上下文必须由 F31 提供。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from psycopg import AsyncConnection

from product_api.specialists import Citation


class KnowledgeAccessError(Exception):
    """知识服务未配置，或操作越过组织、角色或文档边界。"""


class DocumentKind(StrEnum):
    PRODUCT_SUPPLY = "product_supply"
    SALES_GUIDANCE = "sales_guidance"
    BUSINESS_POLICY = "business_policy"


class DocumentVisibility(StrEnum):
    ORGANIZATION = "organization"
    RESTRICTED = "restricted"


class SourceKind(StrEnum):
    OFFICIAL = "official"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class KnowledgeChunkInput:
    ordinal: int
    locator: str
    body: str

    def __post_init__(self) -> None:
        if not 0 <= self.ordinal < 10_000:
            raise ValueError("chunk ordinal 必须在 0–9999")
        if not self.locator or len(self.locator) > 240:
            raise ValueError("chunk locator 必须是 1–240 字符")
        if not self.body or len(self.body) > 20_000:
            raise ValueError("chunk body 必须是 1–20000 字符")

    def as_json(self) -> dict[str, object]:
        return {"ordinal": self.ordinal, "locator": self.locator, "body": self.body}


@dataclass(frozen=True)
class KnowledgeSearchHit:
    document_id: UUID
    document_title: str
    document_kind: DocumentKind
    version_id: UUID
    version_number: int
    chunk_id: UUID
    locator: str
    body: str
    fragment_hash: str
    rank: float

    def citation(self) -> Citation:
        """只传引用标识给 Specialist，不把整段正文藏进 Citation。"""
        return Citation(
            source_kind="knowledge_chunk",
            source_id=str(self.document_id),
            version=str(self.version_id),
            locator=self.locator,
            fragment_hash=self.fragment_hash,
        )


class KnowledgeRepository(Protocol):
    async def create_document(
        self,
        connection: AsyncConnection[dict[str, object]],
        title: str,
        document_kind: DocumentKind,
        visibility: DocumentVisibility,
        source_kind: SourceKind,
        source_url: str | None,
    ) -> UUID: ...

    async def grant_role(
        self,
        connection: AsyncConnection[dict[str, object]],
        document_id: UUID,
        role: str,
    ) -> None: ...

    async def publish_version(
        self,
        connection: AsyncConnection[dict[str, object]],
        document_id: UUID,
        version_label: str,
        source_revision: str,
        normalized_content: str,
        chunks: tuple[KnowledgeChunkInput, ...],
    ) -> UUID: ...

    async def search(
        self,
        connection: AsyncConnection[dict[str, object]],
        query: str,
        limit: int = 20,
    ) -> list[KnowledgeSearchHit]: ...

    async def citation_is_valid(
        self,
        connection: AsyncConnection[dict[str, object]],
        citation: Citation,
    ) -> bool: ...

    async def revoke_version(
        self,
        connection: AsyncConnection[dict[str, object]],
        version_id: UUID,
        reason: str,
    ) -> None: ...

    async def withdraw_document(
        self,
        connection: AsyncConnection[dict[str, object]],
        document_id: UUID,
        reason: str,
    ) -> None: ...


class UnavailableKnowledgeRepository:
    """没有 PostgreSQL/RLS 装配时安全拒绝，不返回内存伪数据。"""

    async def __getattr_call(self, *_: object) -> None:
        raise KnowledgeAccessError("组织知识服务未配置")

    create_document = __getattr_call
    grant_role = __getattr_call
    publish_version = __getattr_call
    search = __getattr_call
    citation_is_valid = __getattr_call
    revoke_version = __getattr_call
    withdraw_document = __getattr_call


class PostgresKnowledgeRepository:
    """只调用 F36 SQL 函数；不自行打开连接或设置组织上下文。"""

    async def create_document(
        self,
        connection: AsyncConnection[dict[str, object]],
        title: str,
        document_kind: DocumentKind,
        visibility: DocumentVisibility,
        source_kind: SourceKind,
        source_url: str | None,
    ) -> UUID:
        return await self._uuid_command(
            connection,
            "select app.create_knowledge_document(%s, %s, %s, %s, %s) as id",
            (title, document_kind.value, visibility.value, source_kind.value, source_url),
        )

    async def grant_role(
        self,
        connection: AsyncConnection[dict[str, object]],
        document_id: UUID,
        role: str,
    ) -> None:
        await self._void_command(
            connection,
            "select app.grant_knowledge_document_role(%s, %s)",
            (document_id, role),
        )

    async def publish_version(
        self,
        connection: AsyncConnection[dict[str, object]],
        document_id: UUID,
        version_label: str,
        source_revision: str,
        normalized_content: str,
        chunks: tuple[KnowledgeChunkInput, ...],
    ) -> UUID:
        if not chunks:
            raise ValueError("知识版本至少需要一个 chunk")
        if len({chunk.ordinal for chunk in chunks}) != len(chunks):
            raise ValueError("chunk ordinal 不能重复")
        if len({chunk.locator for chunk in chunks}) != len(chunks):
            raise ValueError("chunk locator 不能重复")
        return await self._uuid_command(
            connection,
            "select app.publish_knowledge_version(%s, %s, %s, %s, %s::jsonb) as id",
            (
                document_id,
                version_label,
                source_revision,
                normalized_content,
                json.dumps([chunk.as_json() for chunk in chunks], ensure_ascii=False),
            ),
        )

    async def search(
        self,
        connection: AsyncConnection[dict[str, object]],
        query: str,
        limit: int = 20,
    ) -> list[KnowledgeSearchHit]:
        if not query or len(query) > 500 or not 1 <= limit <= 100:
            raise ValueError("query 必须是 1–500 字符，limit 必须是 1–100")
        async with connection.cursor() as cursor:
            await cursor.execute("select * from app.search_knowledge(%s, %s)", (query, limit))
            rows = await cursor.fetchall()
        return [self._hit(row) for row in rows]

    async def citation_is_valid(
        self,
        connection: AsyncConnection[dict[str, object]],
        citation: Citation,
    ) -> bool:
        if citation.source_kind != "knowledge_chunk":
            return False
        try:
            version_id = UUID(citation.version)
        except ValueError:
            return False
        async with connection.cursor() as cursor:
            await cursor.execute(
                "select app.knowledge_citation_is_valid(%s, %s, %s) as valid",
                (version_id, citation.locator, citation.fragment_hash),
            )
            row = await cursor.fetchone()
        return bool(row and row["valid"])

    async def revoke_version(
        self,
        connection: AsyncConnection[dict[str, object]],
        version_id: UUID,
        reason: str,
    ) -> None:
        await self._void_command(
            connection,
            "select app.revoke_knowledge_version(%s, %s)",
            (version_id, reason),
        )

    async def withdraw_document(
        self,
        connection: AsyncConnection[dict[str, object]],
        document_id: UUID,
        reason: str,
    ) -> None:
        await self._void_command(
            connection,
            "select app.withdraw_knowledge_document(%s, %s)",
            (document_id, reason),
        )

    @staticmethod
    async def _uuid_command(
        connection: AsyncConnection[dict[str, object]],
        statement: str,
        parameters: tuple[object, ...],
    ) -> UUID:
        async with connection.cursor() as cursor:
            await cursor.execute(statement, parameters)
            row = await cursor.fetchone()
        if row is None or row.get("id") is None:
            raise KnowledgeAccessError("知识命令没有返回 ID")
        return UUID(str(row["id"]))

    @staticmethod
    async def _void_command(
        connection: AsyncConnection[dict[str, object]],
        statement: str,
        parameters: tuple[object, ...],
    ) -> None:
        async with connection.cursor() as cursor:
            await cursor.execute(statement, parameters)

    @staticmethod
    def _hit(row: dict[str, object]) -> KnowledgeSearchHit:
        return KnowledgeSearchHit(
            document_id=UUID(str(row["document_id"])),
            document_title=str(row["document_title"]),
            document_kind=DocumentKind(str(row["document_kind"])),
            version_id=UUID(str(row["version_id"])),
            version_number=int(str(row["version_number"])),
            chunk_id=UUID(str(row["chunk_id"])),
            locator=str(row["locator"]),
            body=str(row["body"]),
            fragment_hash=str(row["fragment_hash"]),
            rank=float(str(row["rank"])),
        )
