"""F07 substantive hash coverage, partial plans, durable versions and DB failures."""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from procurement_core.offers import Offer, PriceBreak
from procurement_core.plans import build_plan_content, content_hash, generate_plan
from procurement_core.shortage import calculate_shortages

D = Decimal


def example_offer():
    return Offer(component_id="part", distributor="synthetic", distributor_sku="sku",
                 region="US", packaging="cut_tape", currency="USD",
                 retrieved_at=datetime(2026, 9, 14, tzinfo=UTC), moq=D(10), order_multiple=D(5),
                 price_breaks=(PriceBreak(D(1), D("2.5")),), stock_qty=D(200),
                 lead_time_days=3, identity_status="verified", evidence_ref="fixture:offer",
                 is_simulated=True)


def inputs():
    demand = {"demand_id": UUID(int=1), "tenant_id": "default"}
    lines = [{"line_id": "line", "component_id": "part", "required_qty": D(100),
              "unresolved_reason": None}]
    snapshots = [{"snapshot_id": UUID(int=2), "component_id": "part", "required_qty": D(100),
                  "shortage_qty": D(12), "breakdown": {"warnings": []}}]
    return demand, lines, snapshots


@pytest.mark.parametrize("change", [
    {"distributor": "other"}, {"distributor_sku": "other-sku"},
    {"moq": D(20)}, {"order_multiple": D(10)},
    {"price_breaks": (PriceBreak(D(1), D(9)),)}, {"currency": "EUR"},
    {"packaging": "reel"}, {"region": "EU"}, {"lead_time_days": 9},
    {"stock_qty": D(500)}, {"evidence_ref": "fixture:changed"},
])
def test_ev19_substantive_conditions_change_hash(change):
    before = build_plan_content(*inputs(), {"part": example_offer()})
    after = build_plan_content(*inputs(), {"part": replace(example_offer(), **change)})
    assert content_hash(before) != content_hash(after)


def test_hash_ignores_dict_order_and_decimal_scale():
    assert content_hash({"a": D("12.000"), "b": D("3.0")}) == content_hash(
        {"b": D(3), "a": D(12)})


def test_ev05_partial_plan_not_ready():
    demand, lines, snapshots = inputs()
    lines.append({"line_id": "unresolved", "component_id": None, "required_qty": None,
                  "unresolved_reason": "missing_candidate"})
    result = build_plan_content(demand, lines, snapshots, {"part": example_offer()})
    evidence = result["lines"][0]["evidence_ref"]
    assert not evidence["ready_for_review"]
    assert evidence["plan_context"]["unresolved_lines"][0]["line_id"] == "unresolved"
    assert result["is_single_source"]


def test_omitted_snapshot_or_offer_cannot_hide_shortage():
    demand, lines, snapshots = inputs()
    with pytest.raises(ValueError, match="snapshot"):
        build_plan_content(demand, lines, [], {"part": example_offer()})
    with pytest.raises(ValueError, match="explicit offer"):
        build_plan_content(demand, lines, snapshots, {})


@pytest.mark.integration
def test_versions_hash_and_readback(procurement_case):
    repo, _, run_id = procurement_case
    snapshots = calculate_shortages(repo, run_id)["snapshots"]
    args = {"run_id": run_id, "snapshot_ids": [s["snapshot_id"] for s in snapshots],
            "offers": {"part": example_offer()}}
    first = generate_plan(repo, **args)
    second = generate_plan(repo, **args)
    assert (first["version"], second["version"]) == (1, 2)
    assert first["content_hash"] == second["content_hash"]
    assert first["is_single_source"]
    saved = repo.read_plan(first["plan_id"])
    payload = {"hash_version": 1, "demand_id": saved["demand_id"],
               "tenant_id": saved["tenant_id"], "is_single_source": saved["is_single_source"],
               "lines": [{k: v for k, v in line.items() if k != "plan_id"}
                         for line in saved["lines"]]}
    assert content_hash(payload) == saved["content_hash"]
    assert repo.connection.scalar(text("SELECT count(*) FROM permission_decision")) == 0
    assert repo.connection.scalar(text("SELECT count(*) FROM external_action")) == 0
    assert repo.connection.scalar(text("SELECT state FROM run")) == "created"
    third = generate_plan(repo, **{**args, "offers": {"part": replace(example_offer(), moq=D(50))}})
    assert third["version"] == 3 and third["content_hash"] != first["content_hash"]


@pytest.mark.integration
def test_plan_line_failure_rolls_back_header(procurement_case):
    repo, _, run_id = procurement_case
    snapshots = calculate_shortages(repo, run_id)["snapshots"]
    conn = repo.connection
    conn.execute(text("""CREATE FUNCTION fail_plan() RETURNS trigger LANGUAGE plpgsql AS $$
                        BEGIN RAISE EXCEPTION 'injected plan failure'; END $$"""))
    conn.execute(text("""CREATE TRIGGER fail_plan BEFORE INSERT ON plan_line
                        FOR EACH ROW EXECUTE FUNCTION fail_plan()"""))
    with pytest.raises(DBAPIError, match="injected plan failure"):
        generate_plan(repo, run_id=run_id, snapshot_ids=[s["snapshot_id"] for s in snapshots],
                      offers={"part": example_offer()})
    assert conn.scalar(text("SELECT count(*) FROM plan")) == 0
    assert conn.scalar(text("SELECT count(*) FROM plan_line")) == 0


@pytest.mark.integration
def test_concurrent_versions_and_fresh_connection_readback(committed_procurement_case):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from persistence.procurement import ProcurementRepository

    engine, schema, run_id, snapshot_ids = committed_procurement_case
    barrier = Barrier(2, timeout=10)

    def worker():
        with engine.begin() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait()
            return generate_plan(ProcurementRepository(conn), run_id=run_id,
                                 snapshot_ids=snapshot_ids, offers={"part": example_offer()})

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(worker) for _ in range(2)]
        results = [job.result(timeout=15) for job in jobs]
    assert sorted(plan["version"] for plan in results) == [1, 2]
    assert results[0]["content_hash"] == results[1]["content_hash"]
    with engine.begin() as conn:
        conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        repo = ProcurementRepository(conn)
        for plan in results:
            saved = repo.read_plan(plan["plan_id"])
            assert saved["content_hash"] == plan["content_hash"]
            assert len(saved["lines"]) == 1


@pytest.mark.integration
def test_partial_plan_and_unknown_quote_persist_honestly(procurement_case):
    repo, demand_id, run_id = procurement_case
    conn = repo.connection
    conn.execute(text("""INSERT INTO bom_line(line_id,project_id,source_row_index,reference,
                        quantity) VALUES ('unknown','synthetic',2,'R2',1)"""))
    conn.execute(text("""INSERT INTO demand_line(demand_id,line_id,required_qty,unresolved_reason)
                        VALUES (:d,'unknown',NULL,'quantity_basis_unverified')"""), {"d": demand_id})
    snapshots = calculate_shortages(repo, run_id)["snapshots"]
    incomplete = replace(example_offer(), price_breaks=(), stock_qty=None, lead_time_days=None)
    plan = generate_plan(repo, run_id=run_id, snapshot_ids=[s["snapshot_id"] for s in snapshots],
                         offers={"part": incomplete})
    assert not plan["ready_for_review"]
    saved = repo.read_plan(plan["plan_id"])["lines"][0]
    assert saved["unit_price"] is None and saved["currency"] is None
    assert saved["evidence_ref"]["plan_context"]["unresolved_lines"][0]["line_id"] == "unknown"
    assert {"price_unknown", "stock_unknown", "lead_time_unknown"} <= set(
        saved["evidence_ref"]["warnings"])
