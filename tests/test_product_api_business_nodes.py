"""Business-story acceptance of deterministic signals, independent of any LLM."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from product_api.business_nodes import FollowupPolicy, TaskSchedule, followup_signals

AS_OF = datetime(2026, 10, 23, 1, tzinfo=UTC)


def codes(tasks=(), owner=None, stage="negotiation", policy=None):
    return {
        s.code
        for s in followup_signals(
            uuid4(), stage, owner, tasks, AS_OF, policy or FollowupPolicy.load()
        )
    }


def test_new_followup_does_not_erase_old_overdue_task():
    owner = uuid4()
    overdue = TaskSchedule(uuid4(), "open", owner, AS_OF - timedelta(days=1))
    assert codes((overdue,), owner) == {"task_overdue", "no_visible_future_task"}
    future = TaskSchedule(uuid4(), "open", owner, AS_OF + timedelta(days=1))
    assert codes((overdue, future), owner) == {"task_overdue"}
    assert codes((replace(overdue, status="done"), future), owner) == set()


def test_unknown_deadline_unassigned_task_and_exact_deadline_are_distinct():
    task = TaskSchedule(uuid4(), "open", None, None)
    assert codes((task,)) == {
        "opportunity_unassigned",
        "task_unassigned",
        "task_deadline_unknown",
        "no_visible_future_task",
    }
    assert codes((replace(task, assignee_id=uuid4(), due_at=AS_OF),), uuid4()) == set()
    assert codes((replace(task, status="cancelled"),), uuid4()) == {"no_visible_future_task"}


def test_business_policy_is_explicit_and_inactive_opportunities_are_not_scored():
    assert codes(stage="won") == set()
    policy = replace(
        FollowupPolicy.load(), require_opportunity_owner=False, require_future_task=False
    )
    assert codes(policy=policy) == set()
    with pytest.raises(ValueError, match="timezone"):
        followup_signals(uuid4(), "negotiation", None, (), AS_OF.replace(tzinfo=None), policy)


def test_story_demands_and_receipts_are_inputs_not_model_numbers():
    import json
    from pathlib import Path

    story = json.loads(
        (
            Path(__file__).resolve().parents[1] / "data/scenarios/electronics/initial.json"
        ).read_text()
    )
    balances = {
        p["mpn"]: p["onHand"]
        - p["reserved"]
        + p.get("confirmedReceipt", {}).get("quantity", 0)
        - story["board"]["sets"] * p["perSet"]
        for p in story["parts"]
    }
    assert balances == {"SIM-MCU-01": 150, "SIM-RF-SW-01": -200, "SIM-PWR-01": 300}
    assert story["unmappedBusinessInputs"]["providerQuote"]["arrivalConfirmed"] is False
