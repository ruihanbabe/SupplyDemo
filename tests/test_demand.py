"""F03 EV-08 and exact-arithmetic boundary cases with explicit simulated inputs."""
from decimal import Decimal

import pytest

from procurement_core.demand import expand_lines


def bom(verified=True):
    return {"line_id": "line", "quantity": Decimal("0.125"), "qty_basis_verified": verified,
            "candidates": [{"component_id": "part", "identity_status": "verified"}]}


def test_ev08_unverified_quantity_never_multiplied():
    row = bom(False)
    row["quantity"] = None
    assert expand_lines(Decimal(100), [row], {}) == [
        {"line_id": "line", "component_id": None, "required_qty": None,
         "unresolved_reason": "quantity_basis_unverified"}]


def test_exact_fractional_expansion_retains_line():
    result = expand_lines(Decimal(3), [bom()], {"line": "part"})
    assert result[0]["required_qty"] == Decimal("0.375")
    assert result[0]["component_id"] == "part"
    assert result[0]["unresolved_reason"] is None


@pytest.mark.parametrize("value", [1.1, Decimal("NaN"), Decimal("Infinity"),
                                  Decimal(-1), Decimal(0), Decimal("0.0000001"),
                                  Decimal(1000000000000)])
def test_invalid_production_quantity_rejected(value):
    with pytest.raises(ValueError):
        expand_lines(value, [bom()], {"line": "part"})


def test_product_cannot_be_silently_rounded():
    row = bom()
    row["quantity"] = Decimal("0.000001")
    with pytest.raises(ValueError, match="implicit rounding"):
        expand_lines(Decimal("0.000001"), [row], {"line": "part"})


@pytest.mark.integration
def test_uncomputed_null_roundtrip_and_repeat(migrated):
    from datetime import date
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from persistence.import_normalized import import_normalized
    from persistence.procurement import ProcurementRepository
    from procurement_core.demand import prepare_demand

    conn, _, _ = migrated
    import_normalized(conn)
    repo = ProcurementRepository(conn)
    kwargs = {"demand_id": uuid4(), "project_id": "ArdPromSD", "product_version": "source",
              "production_qty": Decimal(100), "need_by_date": date(2026, 12, 1),
              "created_by": "test", "is_simulated": True}
    first = prepare_demand(repo, **kwargs)
    assert not first["ready"]
    assert len(first["lines"]) == 18
    assert all(row["required_qty"] is None for row in repo.demand_lines(kwargs["demand_id"]))
    assert prepare_demand(repo, **kwargs) == first
    component = conn.scalar(text("SELECT component_id FROM component LIMIT 1"))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(text("""UPDATE demand_line SET component_id=:component,
                          unresolved_reason=NULL WHERE demand_id=:id"""),
                     {"component": component, "id": kwargs["demand_id"]})
    with pytest.raises(ValueError, match="different content"):
        prepare_demand(repo, **{**kwargs, "production_qty": Decimal(200)})
    assert conn.scalar(text("SELECT quantity FROM demand")) == 100


@pytest.mark.integration
def test_expansion_atomic_when_selected_candidate_invalid(migrated):
    from datetime import date
    from uuid import uuid4

    from sqlalchemy import text

    from persistence.import_normalized import import_normalized
    from persistence.procurement import ProcurementRepository
    from procurement_core.demand import prepare_demand

    conn, _, _ = migrated
    import_normalized(conn)
    conn.execute(text("UPDATE bom_line SET qty_basis_verified=true"))
    conn.execute(text("UPDATE component SET identity_status='verified'"))
    with pytest.raises(ValueError, match="not a candidate"):
        prepare_demand(ProcurementRepository(conn), demand_id=uuid4(), project_id="ArdPromSD",
                       product_version="source", production_qty=Decimal(100),
                       need_by_date=date(2026, 12, 1), created_by="test", is_simulated=True,
                       selected={"ArdPromSD-001": "not-a-candidate"})
    assert conn.scalar(text("SELECT count(*) FROM demand")) == 0
    assert conn.scalar(text("SELECT count(*) FROM demand_line")) == 0


@pytest.mark.integration
def test_verified_and_unverified_lines_persist_partial_result(migrated):
    from datetime import date
    from uuid import uuid4

    from sqlalchemy import text

    from persistence.import_normalized import import_normalized
    from persistence.procurement import ProcurementRepository
    from procurement_core.demand import prepare_demand

    conn, _, _ = migrated
    import_normalized(conn)
    conn.execute(text("UPDATE bom_line SET qty_basis_verified=true WHERE line_id='ArdPromSD-001'"))
    component = conn.scalar(text("""SELECT component_id FROM bom_line_candidate
                                 WHERE line_id='ArdPromSD-001'"""))
    conn.execute(text("UPDATE component SET identity_status='verified' WHERE component_id=:id"),
                 {"id": component})
    repo = ProcurementRepository(conn)
    result = prepare_demand(repo, demand_id=uuid4(), project_id="ArdPromSD",
                            product_version="source", production_qty=Decimal("12.125"),
                            need_by_date=date(2026, 12, 1), created_by="test", is_simulated=True,
                            selected={"ArdPromSD-001": component})
    assert not result["ready"]
    persisted = repo.demand_lines(result["demand_id"])
    assert persisted[0]["required_qty"] == Decimal("12.125")
    assert persisted[0]["component_id"] == component
    assert sum(row["required_qty"] is None for row in persisted) == 17
