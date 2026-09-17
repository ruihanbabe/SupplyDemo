"""F06 independent tier boundary and uncertainty cases (EV-13/14/12)."""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from procurement_core.offers import Offer, PriceBreak, recommend

D = Decimal


def offer():
    return Offer(component_id="part", distributor="synthetic", distributor_sku="sku",
                 region="US", packaging="cut_tape", currency="USD",
                 retrieved_at=datetime(2026, 9, 14, tzinfo=UTC), moq=D(10), order_multiple=D(5),
                 price_breaks=(PriceBreak(D(1), D(3)), PriceBreak(D(15), D("2.5"))),
                 stock_qty=D(200), lead_time_days=3, identity_status="verified",
                 evidence_ref="fixture:offer-1", is_simulated=True)


def test_ev13_tier_is_selected_after_rounding():
    result = recommend(D(12), offer())
    assert result["suggested_qty"] == 15
    assert result["price_break_qty"] == 15
    assert result["unit_price"] == D("2.5")
    assert result["known_goods_amount"] == D("37.5")
    assert result["complete_quote"]
    assert "tax_and_shipping_excluded" in result["warnings"]


@pytest.mark.parametrize("needed,expected", [(D(0), D(0)), (D(1), D(10)),
                                             (D(10), D(10)), (D(15), D(15)), (D(16), D(20))])
def test_moq_and_multiple_boundaries(needed, expected):
    assert recommend(needed, offer())["suggested_qty"] == expected


def test_missing_fields_are_not_zero_or_complete():
    result = recommend(D(12), replace(offer(), price_breaks=(), stock_qty=None, lead_time_days=None))
    assert result["unit_price"] is None and result["known_goods_amount"] is None
    assert result["currency"] is None
    assert result["offer"]["currency"] == "USD"
    assert not result["complete_quote"]
    assert {"price_unknown", "stock_unknown", "lead_time_unknown"} <= set(result["warnings"])


def test_ev14_currencies_and_packaging_are_preserved_separately():
    usd = recommend(D(12), offer())
    eur = recommend(D(12), replace(offer(), currency="EUR", packaging="reel", region="EU"))
    assert usd["currency"] == "USD" and eur["currency"] == "EUR"
    assert eur["offer"]["packaging"] == "reel"
    assert usd["offer"]["packaging"] == "cut_tape"
    assert "manual_offer_selection" in eur["warnings"]


@pytest.mark.parametrize("change", [{"moq": D(0)}, {"order_multiple": D(0)},
                                    {"currency": None}, {"stock_qty": D(-1)},
                                    {"lead_time_days": -1},
                                    {"retrieved_at": datetime(2026, 9, 14)},  # noqa: DTZ001 -- rejection fixture
                                    {"price_breaks": (PriceBreak(D(1), D(3)),
                                                      PriceBreak(D(1), D(2)))}])
def test_invalid_quote_rejected(change):
    with pytest.raises(ValueError):
        recommend(D(12), replace(offer(), **change))


def test_stock_and_identity_limitations_block_completeness():
    result = recommend(D(12), replace(offer(), stock_qty=D(1), identity_status="review_required"))
    assert not result["complete_quote"]
    assert {"insufficient_stock", "identity_unverified"} <= set(result["warnings"])
