from uuid import uuid4

import pytest
from pydantic import ValidationError

from product_api.context_store import (
    EvidenceRef,
    MessageUnit,
    TranscriptMessage,
    WorkingState,
    WorkingStep,
)


def test_complete_tool_unit_preserves_order_and_rejects_half_pairs():
    invocation = uuid4()
    head = TranscriptMessage(
        role="assistant",
        invocation_id=invocation,
        batch_id=1,
        payload={
            "toolCalls": [
                {"id": "a|raw", "name": "supply.impact", "arguments": {"partNumber": "A"}}
            ]
        },
    )
    result = TranscriptMessage(
        role="tool_result",
        invocation_id=invocation,
        batch_id=1,
        index=0,
        call_id="a|raw",
        payload={"status": "error", "code": "invalid_arguments", "resultRef": None},
    )
    unit = MessageUnit(messages=(head, result))
    assert unit.model_dump(mode="json", by_alias=True)["messages"][1]["callId"] == "a|raw"
    for messages in ((head,), (result,), (head, result.model_copy(update={"index": 1}))):
        with pytest.raises(ValidationError):
            MessageUnit(messages=messages)
    with pytest.raises(ValidationError):
        MessageUnit(messages=(head, result), complete=False)
    assert not MessageUnit(
        messages=(TranscriptMessage(role="control", payload={"state": "interrupted"}),),
        complete=False,
    ).complete


def test_working_state_is_structural_and_does_not_accept_arbitrary_authority():
    state = WorkingState(
        goal="Investigate",
        unknowns=("arrival unconfirmed",),
        steps=(WorkingStep(id="check", status="pending"),),
    )
    assert state.accepted_evidence == ()
    with pytest.raises(ValidationError):
        WorkingState(goal="Investigate", approved=True)
    with pytest.raises(ValidationError):
        WorkingState(
            goal="Investigate",
            steps=(
                WorkingStep(id="same", status="pending"),
                WorkingStep(id="same", status="completed"),
            ),
        )
    with pytest.raises(ValueError):
        MessageUnit.user(" ")


def test_evidence_reference_checks_kind_and_locator():
    for kind, locator in (
        ("trace_artifact", "payload"),
        ("intelligence_observation", "redacted_payload"),
    ):
        with pytest.raises(ValidationError):
            EvidenceRef(
                source_kind=kind,
                source_id=uuid4(),
                version="1",
                locator=locator,
                fragment_hash="a" * 64,
            )
