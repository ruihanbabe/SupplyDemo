from datetime import UTC, datetime
from uuid import UUID

from product_api.crm import CrmConversationFact, CrmOpportunityFact


def test_crm_fact_reference_is_versioned_and_does_not_expose_contact_data() -> None:
    opportunity = CrmOpportunityFact(
        UUID("11111111-1111-1111-1111-111111111111"), "December RF modules", "negotiation", None, 3
    )

    citation = opportunity.reference().citation()

    assert citation.source_kind == "crm_record"
    assert citation.version == "3"
    assert citation.locator == "record"
    assert len(citation.fragment_hash) == 64


def test_crm_conversation_reference_changes_when_versioned_fact_changes() -> None:
    item = CrmConversationFact(
        UUID("22222222-2222-2222-2222-222222222222"), "email", "Customer asked for notice.", 1,
        datetime(2026, 10, 17, tzinfo=UTC),
    )
    changed = CrmConversationFact(item.id, item.channel, item.summary, 2, item.updated_at)

    assert item.reference().fragment_hash != changed.reference().fragment_hash
