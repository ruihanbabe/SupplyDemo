"""Mouser Search API.

Field shapes were read off a live response rather than a schema: the published Swagger
page did not render, and guessing field names is how an adapter silently returns nulls
forever. What the live call showed, for IRFZ44NPBF:

    MouserPartNumber      '942-IRFZ44NPBF'
    Manufacturer          'Infineon Technologies'
    AvailabilityInStock   '0'                      (string, not a number)
    LeadTime              '224 Days'               (string carrying its unit)
    Min / Mult            '1' / '1'                (strings)
    PriceBreaks[0]        {'Quantity': 1, 'Price': '¥10.54', 'Currency': 'RMB'}

The currency is the account's, not a parameter: this key returns RMB. That matters more
than it looks — prices from three distributors in three currencies must not be compared
(BR-03), so the currency travels with every single price.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Offer, PriceBreak, match_status, parse_int, parse_lead_time_days, parse_price

NAME = "mouser"
BASE_URL = "https://api.mouser.com/api/v1"
CREDENTIAL = "SUPPLYAGENT_SUPPLIER_MOUSER_API_KEY"


def configured(environment: dict[str, str]) -> bool:
    return bool(environment.get(CREDENTIAL))


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return ((payload.get("SearchResults") or {}).get("Parts") or [])


def _to_offer(part: dict[str, Any], queried_mpn: str) -> Offer:
    breaks = []
    for tier in part.get("PriceBreaks") or []:
        breaks.append(PriceBreak(
            min_qty=parse_int(tier.get("Quantity")) or 1,
            unit_price=parse_price(tier.get("Price")),
            currency=tier.get("Currency"),
            raw=str(tier.get("Price"))))
    # Mouser reports two stock figures; the one a buyer can have today is the in-stock
    # one. Factory stock is a different fact and is kept raw rather than merged in.
    stock_raw = part.get("AvailabilityInStock")
    lead_raw = part.get("LeadTime")
    return Offer(
        provider=NAME,
        distributor_sku=str(part.get("MouserPartNumber") or ""),
        mpn=str(part.get("ManufacturerPartNumber") or ""),
        manufacturer=str(part.get("Manufacturer") or ""),
        packaging=part.get("Reeling") and "Reel" or None,
        currency=breaks[0].currency if breaks else None,
        moq=parse_int(part.get("Min")),
        order_multiple=parse_int(part.get("Mult")),
        stock_qty=parse_int(stock_raw),
        lead_time_days=parse_lead_time_days(lead_raw),
        lifecycle_status=part.get("LifecycleStatus") or None,
        product_url=part.get("ProductDetailUrl") or None,
        price_breaks=breaks,
        raw={"stock_qty": stock_raw, "lead_time_days": lead_raw,
             "factory_stock": part.get("FactoryStock"),
             "unit_price_at_moq": breaks[0].raw if breaks else None,
             "suggested_replacement": part.get("SuggestedReplacement") or None},
    )


def search(mpn: str, environment: dict[str, str], client: httpx.Client) -> list[dict[str, Any]]:
    response = client.post(
        f"{BASE_URL}/search/partnumber",
        params={"apiKey": environment[CREDENTIAL]},
        json={"SearchByPartRequest": {"mouserPartNumber": mpn,
                                      "partSearchOptions": "Exact"}})
    response.raise_for_status()
    payload = response.json()
    # Mouser answers 200 with an Errors array rather than an HTTP error code, so a
    # failure looks like success unless this is checked.
    errors = payload.get("Errors") or []
    if errors:
        raise RuntimeError(f"mouser: {errors[0].get('Message') or errors[0]}")
    hits = []
    for part in _parts(payload):
        actual = str(part.get("ManufacturerPartNumber") or "")
        hits.append({"distributor_sku": str(part.get("MouserPartNumber") or ""),
                     "mpn": actual,
                     "manufacturer": str(part.get("Manufacturer") or ""),
                     "packaging": None,
                     "match_status": match_status(mpn, actual),
                     "_part": part})
    return hits


def offer(sku: str, environment: dict[str, str], client: httpx.Client,
          *, hint: dict[str, Any] | None = None) -> Offer | None:
    """Reuse the search payload when the caller already has it.

    The search response already contains every field the offer needs, so a second round
    trip would spend quota to learn nothing — and quota here is 1000 calls a day.
    """
    if hint is not None and hint.get("_part"):
        return _to_offer(hint["_part"], hint.get("mpn", ""))
    response = client.post(f"{BASE_URL}/search/partnumber",
                           params={"apiKey": environment[CREDENTIAL]},
                           json={"SearchByPartRequest": {"mouserPartNumber": sku}})
    response.raise_for_status()
    parts = _parts(response.json())
    return _to_offer(parts[0], parts[0].get("ManufacturerPartNumber", "")) if parts else None
