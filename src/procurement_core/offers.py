"""Supplier-neutral quote arithmetic; no provider calls or automatic offer selection."""
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, localcontext

from procurement_core.numbers import quantity


@dataclass(frozen=True)
class PriceBreak:
    min_qty: Decimal
    unit_price: Decimal


@dataclass(frozen=True)
class Offer:
    component_id: str
    distributor: str
    distributor_sku: str
    region: str
    packaging: str
    currency: str | None
    retrieved_at: datetime
    moq: Decimal
    order_multiple: Decimal
    price_breaks: tuple[PriceBreak, ...] = ()
    stock_qty: Decimal | None = None
    lead_time_days: int | None = None
    identity_status: str = "source_asserted"
    evidence_ref: str = ""
    is_simulated: bool = False


def recommend(shortage_qty: Decimal, offer: Offer) -> dict:
    """Round purchasing quantity first, then select its applicable price tier."""
    quantity(shortage_qty)
    quantity(offer.moq, positive=True)
    quantity(offer.order_multiple, positive=True)
    if not all(isinstance(s, str) and s.strip() for s in
               (offer.component_id, offer.distributor, offer.distributor_sku,
                offer.region, offer.packaging, offer.evidence_ref)):
        raise ValueError("Offer identity, region, packaging and evidence are required")
    if offer.retrieved_at.tzinfo is None or offer.retrieved_at.utcoffset() is None:
        raise ValueError("Quote retrieval time must include a timezone")
    if offer.currency is not None and (len(offer.currency) != 3 or
                                      not offer.currency.isalpha() or
                                      not offer.currency.isupper()):
        raise ValueError("Currency must be an uppercase three-letter code")
    if offer.stock_qty is not None:
        quantity(offer.stock_qty)
    if offer.lead_time_days is not None and (type(offer.lead_time_days) is not int or
                                            offer.lead_time_days < 0):
        raise ValueError("Lead time must be a nonnegative integer or unknown")
    seen = set()
    for tier in offer.price_breaks:
        quantity(tier.min_qty)
        quantity(tier.unit_price)
        if tier.min_qty in seen:
            raise ValueError("Duplicate price-break quantity")
        seen.add(tier.min_qty)
    if offer.price_breaks and offer.currency is None:
        raise ValueError("A priced offer must identify its currency")
    with localcontext() as ctx:
        ctx.prec = 60
        suggested = quantity((max(shortage_qty, offer.moq) / offer.order_multiple).to_integral_value(
            rounding=ROUND_CEILING) * offer.order_multiple) if shortage_qty else Decimal(0)
        tiers = [tier for tier in offer.price_breaks if tier.min_qty <= suggested]
        chosen = max(tiers, key=lambda tier: tier.min_qty) if tiers and suggested else None
        amount = quantity(chosen.unit_price * suggested) if chosen else None
    warnings = ["tax_and_shipping_excluded", "manual_offer_selection"]
    if chosen is None and suggested:
        warnings.append("price_unknown")
    if offer.stock_qty is None:
        warnings.append("stock_unknown")
    elif offer.stock_qty < suggested:
        warnings.append("insufficient_stock")
    if offer.lead_time_days is None:
        warnings.append("lead_time_unknown")
    if offer.identity_status != "verified":
        warnings.append("identity_unverified")
    return {"component_id": offer.component_id, "shortage_qty": shortage_qty,
            "suggested_qty": suggested, "unit_price": chosen.unit_price if chosen else None,
            "currency": offer.currency if chosen else None,
            "price_break_qty": chosen.min_qty if chosen else None,
            "known_goods_amount": amount, "offer": asdict(offer), "warnings": warnings,
            "complete_quote": len(warnings) == 2}
