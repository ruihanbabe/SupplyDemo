"""Offline API contract tests: envelope shape, exact numbers, error mapping.

No database. These prove the HTTP layer keeps its promises about serialization and
status mapping; whether the arithmetic underneath is right is F03–F07's job, and
whether it survives real storage is the integration layer's.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import NoResultFound

from api.dependencies import get_repository
from api.main import app

DEMAND_ID = uuid4()


class StubRepository:
    """Minimal stand-in. Raises the same exception types the real repository raises."""

    def __init__(self, **overrides):
        self.overrides = overrides
        self.saved_lines = None

    @contextmanager
    def atomic(self):
        yield

    def list_projects(self):
        return self.overrides.get("projects", [{"project_id": "demo", "source_file": "x.csv",
                                                "bom_lines": 1, "total_quantity": 2}])

    def bom(self, project_id):
        return self.overrides.get("bom", [])

    def create_demand(self, values):
        return values

    def save_demand_lines(self, demand_id, rows):
        self.saved_lines = rows

    def read_demand(self, demand_id):
        if "demand" not in self.overrides:
            raise NoResultFound
        return self.overrides["demand"]

    def demand_lines(self, demand_id):
        return self.overrides.get("lines", [])

    def demand_plans(self, demand_id):
        return self.overrides.get("plans", [])

    def read_plan(self, plan_id):
        if "plan" not in self.overrides:
            raise NoResultFound
        return self.overrides["plan"]


@contextmanager
def client(**overrides):
    app.dependency_overrides[get_repository] = lambda: StubRepository(**overrides)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def envelope_keys(body):
    return set(body) == {"schema_version", "status", "data", "warnings", "error", "trace_id"}


def iter_scalars(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_scalars(item)
    else:
        yield value


# ---------- 包络与追踪 ----------

def test_health_returns_complete_envelope():
    with client() as api:
        response = api.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert envelope_keys(body)
    assert body["status"] == "ok"
    assert body["error"] is None
    assert body["schema_version"] == 1


def test_trace_id_is_echoed_and_honoured():
    with client() as api:
        generated = api.get("/health")
        supplied = api.get("/health", headers={"x-trace-id": "caller-supplied-id"})
    assert generated.json()["trace_id"] == generated.headers["x-trace-id"]
    assert supplied.json()["trace_id"] == "caller-supplied-id"
    assert supplied.headers["x-trace-id"] == "caller-supplied-id"


# ---------- BR-03：精确数值不得经过 float ----------

def test_response_never_contains_json_floats():
    """A float anywhere in the payload means a quantity lost precision in transit."""
    plan = {"plan_id": uuid4(), "version": 1, "content_hash": "abc", "is_single_source": True,
            "lines": [{"component_id": "p", "shortage_qty": Decimal("30.000001"),
                       "unit_price": Decimal("0.1"), "suggested_qty": Decimal(15)}]}
    with client(plan=plan) as api:
        response = api.get(f"/api/plans/{uuid4()}")
    body = response.json()
    assert not any(isinstance(v, float) for v in iter_scalars(body))
    line = body["data"]["plan"]["lines"][0]
    assert line["shortage_qty"] == "30.000001"
    assert line["unit_price"] == "0.1"
    # Re-parsing the raw text with Decimal must not find a float either.
    assert "30.000001" in response.text


def test_float_quantity_in_request_is_rejected():
    payload = {"project_id": "demo", "product_version": "v1", "production_qty": 100.5,
               "need_by_date": "2026-12-01", "created_by": "tester"}
    with client() as api:
        response = api.post("/api/demands", content=json.dumps(payload),
                            headers={"content-type": "application/json"})
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "validation_failed"
    assert "floats lose precision" in body["error"]["message"]


def test_string_quantity_is_accepted():
    payload = {"project_id": "demo", "product_version": "v1", "production_qty": "100.5",
               "need_by_date": "2026-12-01", "created_by": "tester"}
    with client(bom=[{"line_id": "l1", "quantity": Decimal(1), "qty_basis_verified": True,
                      "candidates": [{"component_id": "c1", "manufacturer": "m", "mpn": "x",
                                      "identity_status": "verified"}]}]) as api:
        response = api.post("/api/demands", json=payload | {"selected": {"l1": "c1"}})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["lines"][0]["required_qty"] == "100.5"


# ---------- 入参严格性 ----------

def test_unknown_field_is_rejected():
    payload = {"project_id": "demo", "product_version": "v1", "production_qty": "10",
               "need_by_date": "2026-12-01", "created_by": "t", "urgency": "high"}
    with client() as api:
        response = api.post("/api/demands", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_failed"


def test_naive_quote_timestamp_is_rejected():
    body = {"snapshot_ids": [str(uuid4())],
            "offers": {"c1": {"component_id": "c1", "distributor": "d", "distributor_sku": "s",
                              "region": "US", "packaging": "reel", "evidence_ref": "ev",
                              "retrieved_at": "2026-09-18T10:00:00", "moq": "10",
                              "order_multiple": "5"}}}
    with client() as api:
        response = api.post(f"/api/runs/{uuid4()}/plans", json=body)
    assert response.status_code == 400
    assert "timezone" in response.json()["error"]["message"]


# ---------- 错误映射 ----------

def test_missing_resource_maps_to_404():
    with client() as api:
        response = api.get(f"/api/demands/{DEMAND_ID}")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["data"] is None


def test_core_rule_violation_maps_to_400():
    """prepare_demand rejects a project with no BOM lines; that is a 400, not a 500."""
    with client(bom=[]) as api:
        response = api.post("/api/demands", json={
            "project_id": "empty", "product_version": "v1", "production_qty": "10",
            "need_by_date": "2026-12-01", "created_by": "tester"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "no BOM lines" in body["error"]["message"]


# ---------- 未解决项如实上报（BR-10） ----------

def test_unresolved_lines_downgrade_status_to_partial():
    with client(demand={"demand_id": DEMAND_ID, "quantity": Decimal(10)},
                lines=[{"line_id": "a", "unresolved_reason": None, "required_qty": Decimal(1)},
                       {"line_id": "b", "unresolved_reason": "missing_candidate",
                        "required_qty": None}]) as api:
        response = api.get(f"/api/demands/{DEMAND_ID}")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "partial"
    assert body["data"]["ready"] is False
    assert len(body["data"]["unresolved_lines"]) == 1
    assert "unresolved_lines_require_confirmation" in body["warnings"]


def test_bom_warnings_name_each_blocking_condition():
    bom = [
        {"line_id": "a", "qty_basis_verified": False, "candidates": []},
        {"line_id": "b", "qty_basis_verified": True,
         "candidates": [{"component_id": "x"}, {"component_id": "y"}]},
    ]
    with client(bom=bom) as api:
        response = api.get("/api/projects/demo/bom")
    warnings = response.json()["warnings"]
    assert "quantity_basis_unverified_lines_present" in warnings
    assert "lines_without_candidates_present" in warnings
    assert "multi_candidate_lines_require_selection" in warnings


def test_empty_result_is_not_found_not_an_error():
    """EV-09's principle at the HTTP layer: nothing found is not the same as failure."""
    with client(projects=[]) as api:
        response = api.get("/api/projects")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "not_found"
    assert body["error"] is None
    assert body["data"]["projects"] == []


# ---------- 包络自身的不变量 ----------

def test_envelope_rejects_inconsistent_error_state():
    from api.envelope import envelope

    with pytest.raises(ValueError):
        envelope(None, status="error")
    with pytest.raises(ValueError):
        envelope(None, status="ok", error={"code": "x", "message": "y", "retryable": False})
    with pytest.raises(ValueError):
        envelope(None, status="unknown_status")


def test_envelope_serializes_dates_and_uuids():
    from api.envelope import envelope

    body = envelope({"when": date(2026, 12, 1), "who": DEMAND_ID, "qty": Decimal("1.500000")})
    assert body["data"] == {"when": "2026-12-01", "who": str(DEMAND_ID), "qty": "1.500000"}
