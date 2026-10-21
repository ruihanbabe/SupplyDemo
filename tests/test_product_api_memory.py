from uuid import uuid4

import pytest

from product_api.memory import LongTermMemory, MemoryStatus


def test_model_candidate_requires_human_confirmation_before_it_is_usable() -> None:
    memory = LongTermMemory.model_candidate(uuid4(), {"fact": "customer prefers weekly updates"})

    assert memory.status is MemoryStatus.CANDIDATE
    assert not memory.usable

    confirmed = memory.confirmed(uuid4())

    assert confirmed.status is MemoryStatus.CONFIRMED
    assert confirmed.usable


def test_revoke_and_revalidation_make_confirmed_memory_unusable() -> None:
    memory = LongTermMemory.model_candidate(uuid4(), {"fact": "approval is required"}).confirmed(
        uuid4()
    )

    assert memory.pending_revalidation().status is MemoryStatus.PENDING_REVALIDATION
    assert not memory.pending_revalidation().usable
    assert memory.revoked().status is MemoryStatus.REVOKED


def test_model_candidate_rejects_empty_or_oversized_content() -> None:
    with pytest.raises(ValueError, match="content"):
        LongTermMemory.model_candidate(uuid4(), {})
    with pytest.raises(ValueError, match="content"):
        LongTermMemory.model_candidate(uuid4(), {"fact": "x" * 16_001})
