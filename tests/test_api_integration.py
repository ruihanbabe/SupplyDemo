"""API against real PostgreSQL: full flow, exact numbers through storage, rollback.

The offline suite proves the HTTP layer's promises in isolation. These prove they still
hold once a real transaction, real NUMERIC columns and real JSONB are in the path.
"""
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from api.dependencies import get_repository
from api.main import app
from infrastructure.database import database_url
from persistence.procurement import ProcurementRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def api(procurement_case):
    """Shares the fixture's transaction, so every write is rolled back afterwards."""
    repository = procurement_case[0]
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        yield TestClient(app), procurement_case
    finally:
        app.dependency_overrides.clear()


@contextmanager
def committed_api(schema):
    """Production dependency semantics: own connection, own transaction, real commit."""
    engine = create_engine(database_url(), hide_parameters=True)

    def dependency():
        # begin() first: an execute() would autobegin and make begin() raise. SET LOCAL
        # reverts when the transaction ends, so the pooled connection stays clean.
        with engine.connect() as connection, connection.begin():
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            yield ProcurementRepository(connection)

    app.dependency_overrides[get_repository] = dependency
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_full_flow_over_http(api):
    """EV-01 end to end through the API: 100 required, 60 on hand, 10 held, 20 inbound."""
    client, (_, _demand_id, run_id) = api

    shortages = client.post(f"/api/runs/{run_id}/shortages")
    assert shortages.status_code == 200, shortages.text
    body = shortages.json()
    snapshot = body["data"]["snapshots"][0]
    # NUMERIC(18,6) round-trips at its declared scale; these are exact, not padded.
    assert snapshot["shortage_qty"] == "30.000000"
    assert snapshot["required_qty"] == "100.000000"
    assert snapshot["allocatable_qty"] == "70.000000"

    offer = {"component_id": "part", "distributor": "digikey", "distributor_sku": "SKU-1",
             "region": "US", "packaging": "reel", "evidence_ref": "ev-1",
             "retrieved_at": "2026-09-18T10:00:00+00:00", "moq": "10", "order_multiple": "5",
             "currency": "USD", "price_breaks": [{"min_qty": "1", "unit_price": "2"},
                                                 {"min_qty": "25", "unit_price": "1.5"}],
             "stock_qty": "500", "lead_time_days": 7, "identity_status": "verified"}
    created = client.post(f"/api/runs/{run_id}/plans", json={
        "snapshot_ids": [snapshot["snapshot_id"]], "offers": {"part": offer}})
    assert created.status_code == 200, created.text
    plan = created.json()["data"]
    line = plan["lines"][0]
    # Shortage 30 already clears MOQ 10 and is a multiple of 5, so 30 stands; the tier is
    # the one 30 falls into (min_qty 25), not the one the raw shortage would suggest.
    # Plan lines are the canonical form that was hashed, where trailing zeros are
    # stripped so the hash does not change with a column's scale. Snapshot readback
    # above keeps the storage scale. Both are exact; the difference is deliberate.
    assert line["suggested_qty"] == "30"
    assert line["price_break_qty"] == "25"
    assert line["unit_price"] == "1.5"

    readback = client.get(f"/api/plans/{plan['plan_id']}")
    assert readback.status_code == 200
    stored = readback.json()["data"]["plan"]
    assert stored["content_hash"] == plan["content_hash"]
    assert stored["version"] == 1
    assert stored["lines"][0]["suggested_qty"] == "30"
    assert stored["lines"][0]["unit_price"] == "1.5"


def test_quantities_survive_storage_as_exact_strings(api):
    client, (_, _, run_id) = api
    response = client.post(f"/api/runs/{run_id}/shortages")
    # Quoted, so the value never passed through a JSON number on the way out.
    assert '"shortage_qty":"30.000000"' in response.text.replace(" ", "")

    def scalars(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from scalars(item)
        elif isinstance(value, list):
            for item in value:
                yield from scalars(item)
        else:
            yield value

    assert not any(isinstance(v, float) for v in scalars(response.json()))


def test_shortage_reports_resource_competition(api):
    """EV-02: another demand holds stock, so the answer must say so rather than hide it."""
    client, (_, _, run_id) = api
    body = client.post(f"/api/runs/{run_id}/shortages").json()
    assert "resource_competition" in body["warnings"]
    assert "advisory_only_no_reservation" in body["warnings"]
    assert "recheck_before_execution" in body["warnings"]


def test_run_creation_inherits_demand_tenant(api):
    client, (_, demand_id, _) = api
    response = client.post("/api/runs", json={"demand_id": str(demand_id)})
    assert response.status_code == 200, response.text
    run = response.json()["data"]["run"]
    assert run["tenant_id"] == "default"
    assert run["state"] == "created"
    assert run["trigger_kind"] == "user"


def test_unknown_run_maps_to_404(api):
    client, _ = api
    response = client.post(f"/api/runs/{uuid4()}/shortages")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_failed_request_writes_nothing(committed_procurement_case):
    """A rejected demand must not leave its header row behind.

    prepare_demand inserts the demand before it discovers the project has no BOM lines,
    so this only passes if the request-scoped transaction actually rolls back.
    """
    engine, schema, _, _ = committed_procurement_case
    with engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        conn.execute(text("""INSERT INTO project(project_id,source_file,bom_lines,total_quantity)
                             VALUES ('no-bom','fixture',0,0)"""))
        before = conn.scalar(text("SELECT count(*) FROM demand"))

    with committed_api(schema) as client:
        response = client.post("/api/demands", json={
            "project_id": "no-bom", "product_version": "v1", "production_qty": "10",
            "need_by_date": "2026-12-01", "created_by": "tester"})

    assert response.status_code == 400
    assert "no BOM lines" in response.json()["error"]["message"]
    with engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        assert conn.scalar(text("SELECT count(*) FROM demand")) == before


def test_successful_request_commits(committed_procurement_case):
    """The mirror of the rollback test: a clean request must actually persist."""
    engine, schema, run_id, _snapshots = committed_procurement_case
    with committed_api(schema) as client:
        response = client.post("/api/runs", json={"demand_id": str(
            _demand_of(engine, schema, run_id))})
    assert response.status_code == 200, response.text
    new_run = response.json()["data"]["run"]["run_id"]
    with engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        assert conn.scalar(text("SELECT count(*) FROM run WHERE run_id=:id"), {"id": new_run}) == 1


def _demand_of(engine, schema, run_id):
    with engine.begin() as conn:
        conn.execute(text(f'SET search_path TO "{schema}"'))
        return conn.scalar(text("SELECT demand_id FROM run WHERE run_id=:id"), {"id": run_id})
