"""F04 EV-01/02 arithmetic, uncertainty, persisted evidence and failure atomicity."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from procurement_core.shortage import calculate_shortages, compute_shortage

D = Decimal
DAY = date(2026, 12, 1)


def sources():
    return ([{"warehouse": "main", "on_hand_qty": D(60), "quarantine_qty": D(999)}],
            [{"demand_id": "other", "component_id": "p", "allocated_qty": D(10)}],
            [{"in_transit_id": "t", "qty": D(20), "is_confirmed": True,
              "eta": DAY, "allocated_to": None}])


def test_ev01_gap_is_30_and_duplicate_records_do_not_double_count():
    inventory, allocations, transit = sources()
    result = compute_shortage(D(100), "d", DAY, inventory * 2, allocations * 2, transit * 2)
    assert result["shortage_qty"] == 30
    assert result["allocatable_qty"] == 70
    assert result["breakdown"]["quarantine_excluded"]
    assert len(result["breakdown"]["eligible_transit"]) == 1


@pytest.mark.parametrize("change,reason", [({"eta": None}, "eta_unknown"),
    ({"eta": date(2026, 12, 2)}, "after_need_by_date"),
    ({"is_confirmed": False}, "unconfirmed"),
    ({"allocated_to": "another"}, "allocated_to_other_demand")])
def test_ineligible_transit_is_explained(change, reason):
    inv, alloc, transit = sources()
    transit[0].update(change)
    result = compute_shortage(D(100), "d", DAY, inv, alloc, transit)
    assert result["shortage_qty"] == 50
    assert result["breakdown"]["excluded_transit"][0]["excluded_reason"] == reason


def test_own_allocations_are_counted_only_once():
    inv, alloc, transit = sources()
    alloc.append({"demand_id": "d", "component_id": "p", "allocated_qty": D(20)})
    transit[0]["allocated_to"] = "d"
    result = compute_shortage(D(100), "d", DAY, inv, alloc, transit)
    assert result["shortage_qty"] == 30
    assert result["breakdown"]["unmet_qty"] == 80
    assert result["allocatable_qty"] == 50


def test_conflicting_inventory_snapshots_rejected():
    inv, alloc, transit = sources()
    with pytest.raises(ValueError, match="Conflicting"):
        compute_shortage(D(100), "d", DAY, inv + [{**inv[0], "on_hand_qty": D(70)}], alloc, transit)


def test_missing_inventory_stays_unknown_and_gap_never_negative():
    result = compute_shortage(D(10), "d", DAY, [], [], [])
    assert "inventory_snapshot_missing" in result["breakdown"]["warnings"]
    inv, alloc, transit = sources()
    assert compute_shortage(D(10), "d", DAY, inv, alloc, transit)["shortage_qty"] == 0


@pytest.mark.integration
def test_persisted_gap_and_advisory_no_stock_writes(procurement_case):
    repo, _, run_id = procurement_case
    conn = repo.connection
    source_tables = ("inventory", "inventory_allocation", "in_transit")
    before = {t: conn.execute(text(f"SELECT * FROM {t}")).all() for t in source_tables}
    result = calculate_shortages(repo, run_id)
    assert result["ready"]
    assert result["snapshots"][0]["shortage_qty"] == 30
    saved = conn.execute(text("SELECT * FROM shortage_snapshot")).mappings().one()
    assert saved["breakdown"]["on_hand_qty"] == "60.000000"
    assert saved["breakdown"]["is_simulated"]
    assert saved["breakdown"]["demand_lines"][0]["line_id"] == "line"
    calculate_shortages(repo, run_id)
    assert {t: conn.execute(text(f"SELECT * FROM {t}")).all() for t in source_tables} == before
    assert conn.scalar(text("SELECT count(*) FROM shortage_snapshot")) == 2


@pytest.mark.integration
def test_shortage_insert_failure_rolls_back(procurement_case):
    repo, _, run_id = procurement_case
    conn = repo.connection
    conn.execute(text("""CREATE FUNCTION fail_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'injected snapshot failure'; END $$"""))
    conn.execute(text("""CREATE TRIGGER fail_snapshot BEFORE INSERT ON shortage_snapshot
                        FOR EACH ROW EXECUTE FUNCTION fail_snapshot()"""))
    with pytest.raises(DBAPIError, match="injected snapshot failure"):
        calculate_shortages(repo, run_id)
    assert conn.scalar(text("SELECT count(*) FROM shortage_snapshot")) == 0
    conn.execute(text("DROP TRIGGER fail_snapshot ON shortage_snapshot"))
    assert calculate_shortages(repo, run_id)["snapshots"][0]["shortage_qty"] == 30
