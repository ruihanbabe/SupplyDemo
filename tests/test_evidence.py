"""F12: observations are stored once, never edited, and judged fresh at read time."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from contracts.evidence import FieldLocator, Observation
from persistence.evidence import EvidenceLedger, content_hash
from procurement_core.freshness import FreshnessPolicy, judge

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def observation(**overrides) -> Observation:
    base = Observation(
        run_id=uuid4(), kind="stock", subject_ref="part", attribute="quantity_available",
        value_raw="500", retrieved_at=NOW, provenance="sample",
        locator=FieldLocator(source_name="digikey", request_digest="d1",
                             field_path="Products[0].QuantityAvailable"))
    return replace(base, **overrides)


# ---------- 观察的身份 ----------

def test_same_question_same_answer_is_one_observation():
    """Two nodes racing on the same lookup made one observation, not two."""
    first = observation()
    second = replace(first, retrieved_at=NOW + timedelta(seconds=3))
    assert content_hash(first) == content_hash(second)


def test_same_value_from_two_sources_stays_two_observations():
    """BR-06: disagreement is preserved. Merging by value alone would erase which
    source said what, and multi-source divergence is a finding, not noise."""
    digikey = observation()
    mouser = replace(digikey, locator=FieldLocator(
        source_name="mouser", request_digest="d2", field_path="Stock"))
    assert content_hash(digikey) != content_hash(mouser)


def test_a_different_answer_is_a_different_observation():
    assert content_hash(observation()) != content_hash(observation(value_raw="480"))


def test_provenance_takes_part_in_identity():
    """A cached read and a live read of the same value are not interchangeable."""
    assert content_hash(observation()) != content_hash(observation(provenance="real"))


# ---------- 新鲜度：三态 ----------

def test_freshness_has_three_outcomes_not_two():
    policy = FreshnessPolicy(windows={"stock": timedelta(hours=24)}, version=1)
    assert judge(NOW, "stock", policy, NOW + timedelta(hours=2)) == "usable"
    assert judge(NOW, "stock", policy, NOW + timedelta(hours=30)) == "stale"
    # No window configured for this kind: nobody said how old is too old. Reporting that
    # as "stale" would send someone re-fetching data that was never the problem.
    assert judge(NOW, "price", policy, NOW + timedelta(hours=30)) == "unknown"


def test_no_policy_means_unknown_not_usable():
    assert judge(NOW, "stock", FreshnessPolicy(windows={}), NOW) == "unknown"


def test_policy_carries_the_rule_version_it_came_from():
    policy = FreshnessPolicy.from_rule({"version": 7, "value": {"hours": {"stock": 12}}})
    assert policy.version == 7
    assert policy.windows["stock"] == timedelta(hours=12)


# ---------- 真库：只插入、去重、取代 ----------

@pytest.fixture
def ledger_case(migrated, procurement_case):
    repo, _, run_id = procurement_case
    # The app role needs to reach the test's temporary schema before its table grants
    # can be exercised at all; without this a denial is indistinguishable from the
    # table not existing.
    migrated[0].execute(text(f'GRANT USAGE ON SCHEMA "{migrated[2]}" TO supplyagent_app'))
    return EvidenceLedger(repo.connection), run_id, repo.connection


@pytest.mark.integration
def test_recording_twice_stores_one_row(ledger_case):
    ledger, run_id, conn = ledger_case
    first_id, created_first = ledger.record(observation(run_id=run_id))
    second_id, created_second = ledger.record(
        observation(run_id=run_id, retrieved_at=NOW + timedelta(seconds=5)))
    assert created_first is True
    assert created_second is False
    assert first_id == second_id
    assert conn.scalar(text("SELECT count(*) FROM evidence WHERE run_id = :r"),
                       {"r": run_id}) == 1


@pytest.mark.integration
def test_recorded_value_cannot_be_edited(ledger_case):
    """Append-only is enforced by grants, not by the application remembering to."""
    ledger, run_id, conn = ledger_case
    evidence_id, _ = ledger.record(observation(run_id=run_id))
    conn.execute(text("SET LOCAL ROLE supplyagent_app"))
    for sql in ("UPDATE evidence SET value_raw = '0'",
                "UPDATE evidence SET retrieved_at = now()",
                "DELETE FROM evidence"):
        with pytest.raises(DBAPIError) as error, conn.begin_nested():
            conn.execute(text(sql))
        assert error.value.orig.sqlstate == "42501"
    # The one narrow exception, granted per column.
    conn.execute(text("UPDATE evidence SET superseded_by = NULL WHERE evidence_id = :e"),
                 {"e": evidence_id})
    conn.execute(text("RESET ROLE"))


@pytest.mark.integration
def test_superseding_keeps_the_old_row_readable(ledger_case):
    """What was known at approval time must stay answerable afterwards."""
    ledger, run_id, conn = ledger_case
    old_id, _ = ledger.record(observation(run_id=run_id))
    new_id = ledger.supersede(old_id, observation(run_id=run_id, value_raw="120"))
    rows = {row["evidence_id"]: row for row in conn.execute(
        text("SELECT * FROM evidence WHERE run_id = :r"), {"r": run_id}).mappings()}
    assert rows[old_id]["value_raw"] == "500"          # untouched
    assert rows[old_id]["superseded_by"] == new_id
    assert rows[new_id]["superseded_by"] is None
    current = ledger.for_subject(run_id, "part")
    assert [row["evidence_id"] for row in current] == [new_id]
    assert len(ledger.for_subject(run_id, "part", include_superseded=True)) == 2


@pytest.mark.integration
def test_locator_travels_with_the_observation(ledger_case):
    ledger, run_id, _ = ledger_case
    ledger.record(observation(run_id=run_id))
    row = ledger.for_subject(run_id, "part", kind="stock")[0]
    assert row["source_name"] == "digikey"
    assert row["field_path"] == "Products[0].QuantityAvailable"
    assert row["response_status"] == "ok"
    # Normalisation was not attempted, so the column stays empty rather than guessing.
    assert row["value_normalized"] is None


@pytest.mark.integration
def test_observed_at_stays_empty_when_the_source_did_not_state_one(ledger_case):
    """Substituting retrieved_at would turn 'unknown' into 'known'."""
    ledger, run_id, _ = ledger_case
    ledger.record(observation(run_id=run_id))
    assert ledger.for_subject(run_id, "part")[0]["observed_at"] is None
