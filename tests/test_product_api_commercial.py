"""Exact BOM quantities and quote lifecycle boundaries do not depend on a model."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from product_api.commercial import BomLine, QuoteFact, bom_requirements, quote_validity

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def quote():
    return QuoteFact(
        uuid4(),
        "DEMO",
        1,
        uuid4(),
        "supplier_offer",
        "2",
        Decimal(200),
        Decimal("1.250001"),
        "USD",
        NOW - timedelta(days=1),
        NOW + timedelta(days=1),
        None,
    )


def test_bom_revision_quantities_are_exact_and_same_part_lines_aggregate():
    lines = (
        BomLine("1", "Demo", "RF", Decimal("0.100001")),
        BomLine("2", "Demo", "RF", Decimal("0.200002")),
    )
    assert bom_requirements(lines, Decimal(500))[0].quantity == Decimal("150.001500")
    large = bom_requirements(
        (BomLine("1", "Demo", "RF", Decimal("999999999999.999999")),),
        Decimal("999999999999.999999"),
    )[0].quantity
    assert str(large) == "999999999999999998000000.000000000001"
    with pytest.raises(ValueError, match="manufacturer"):
        bom_requirements((lines[0], replace(lines[1], manufacturer="Other")), Decimal(1))
    with pytest.raises(ValueError, match="duplicate"):
        bom_requirements((lines[0], lines[0]), Decimal(1))


def test_quote_expiry_is_exclusive_and_missing_terms_are_unknown():
    q = quote()
    assert quote_validity(q, NOW, q.bom_revision_id, 1) == "within_validity_window"
    assert quote_validity(q, q.valid_until, q.bom_revision_id, 1) == "expired"
    assert (
        quote_validity(replace(q, valid_until=None), NOW, q.bom_revision_id, 1)
        == "validity_unknown"
    )
    assert q.unit_price == Decimal("1.250001")
    assert q.confirmed_arrival_on is None and not q.stock_reserved


def test_quote_revision_future_withdrawal_and_bom_applicability_are_distinct():
    q = quote()
    assert quote_validity(q, NOW, q.bom_revision_id, 2) == "superseded"
    assert quote_validity(q, NOW, uuid4(), 1) == "bom_mismatch"
    assert (
        quote_validity(replace(q, issued_at=NOW + timedelta(hours=1)), NOW, q.bom_revision_id, 1)
        == "not_yet_valid"
    )
    assert quote_validity(replace(q, revoked_at=NOW), NOW, q.bom_revision_id, 1) == "withdrawn"
    with pytest.raises(ValueError, match="timezone"):
        quote_validity(q, NOW.replace(tzinfo=None), q.bom_revision_id, 1)
