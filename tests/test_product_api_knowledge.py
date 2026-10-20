import asyncio
from uuid import UUID, uuid4

import pytest

from product_api.knowledge import (
    DocumentKind,
    KnowledgeAccessError,
    KnowledgeChunkInput,
    KnowledgeSearchHit,
    PostgresKnowledgeRepository,
    UnavailableKnowledgeRepository,
)


def test_chunk_input_and_publish_shape_are_bounded() -> None:
    chunk = KnowledgeChunkInput(0, "page:1", "MAX2837 is listed in the official document.")
    assert chunk.as_json() == {
        "ordinal": 0,
        "locator": "page:1",
        "body": "MAX2837 is listed in the official document.",
    }
    with pytest.raises(ValueError, match="ordinal"):
        KnowledgeChunkInput(-1, "page:1", "x")
    with pytest.raises(ValueError, match="locator"):
        KnowledgeChunkInput(0, "", "x")
    with pytest.raises(ValueError, match="body"):
        KnowledgeChunkInput(0, "page:1", "")


def test_search_hit_emits_a_minimal_knowledge_citation() -> None:
    document_id, version_id = uuid4(), uuid4()
    hit = KnowledgeSearchHit(
        document_id=document_id,
        document_title="HackRF components",
        document_kind=DocumentKind.PRODUCT_SUPPLY,
        version_id=version_id,
        version_number=1,
        chunk_id=uuid4(),
        locator="section:r9",
        body="MAX2837 was replaced by MAX2839.",
        fragment_hash="a" * 64,
        rank=0.5,
    )

    citation = hit.citation()

    assert citation.source_kind == "knowledge_chunk"
    assert citation.source_id == str(document_id)
    assert citation.version == str(version_id)
    assert citation.locator == "section:r9"
    assert not hasattr(citation, "body")


def test_invalid_or_non_knowledge_citation_is_rejected_without_sql() -> None:
    repository = PostgresKnowledgeRepository()
    hit = KnowledgeSearchHit(
        document_id=uuid4(),
        document_title="x",
        document_kind=DocumentKind.PRODUCT_SUPPLY,
        version_id=uuid4(),
        version_number=1,
        chunk_id=uuid4(),
        locator="page:1",
        body="x",
        fragment_hash="b" * 64,
        rank=1.0,
    )
    citation = hit.citation()
    other = type(citation)("crm", citation.source_id, citation.version, citation.locator, "b" * 64)
    malformed = type(citation)(
        "knowledge_chunk", citation.source_id, "not-a-uuid", citation.locator, "b" * 64
    )

    assert asyncio.run(repository.citation_is_valid(None, other)) is False  # type: ignore[arg-type]
    assert asyncio.run(repository.citation_is_valid(None, malformed)) is False  # type: ignore[arg-type]


def test_search_hit_rejects_unknown_document_kind() -> None:
    row: dict[str, object] = {
        "document_id": uuid4(),
        "document_title": "x",
        "document_kind": "unknown",
        "version_id": uuid4(),
        "version_number": 1,
        "chunk_id": uuid4(),
        "locator": "page:1",
        "body": "x",
        "fragment_hash": "c" * 64,
        "rank": 1,
    }
    with pytest.raises(ValueError):
        PostgresKnowledgeRepository._hit(row)


def test_unavailable_repository_fails_closed() -> None:
    repository = UnavailableKnowledgeRepository()
    with pytest.raises(KnowledgeAccessError, match="未配置"):
        asyncio.run(repository.search(None, "MAX2837"))  # type: ignore[arg-type]


def test_hit_uuid_conversion_is_strict() -> None:
    row: dict[str, object] = {
        "document_id": UUID(int=1),
        "document_title": "x",
        "document_kind": "product_supply",
        "version_id": UUID(int=2),
        "version_number": "1",
        "chunk_id": UUID(int=3),
        "locator": "page:1",
        "body": "x",
        "fragment_hash": "d" * 64,
        "rank": "0.25",
    }
    assert PostgresKnowledgeRepository._hit(row).rank == 0.25
