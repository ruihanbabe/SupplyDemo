"""DigiKey Product Information V4.

Read off a live response for IRFZ44NPBF:

    ManufacturerProductNumber  'IRFZ44NPBF'
    QuantityAvailable          6484                       (a number, unlike the others)
    UnitPrice                  1.44
    Manufacturer               {'Id': 448, 'Name': 'Infineon Technologies'}
    ProductStatus              {'Id': 0, 'Status': 'Active'}
    ManufacturerLeadWeeks      — lead time in WEEKS, not days
    ProductVariations[0]       {'DigiKeyProductNumber': 'IRFZ44NPBF-ND',
                                'PackageType': {'Name': 'Tube'},
                                'MinimumOrderQuantity': 1,
                                'StandardPricing': [{'BreakQuantity': 1, 'UnitPrice': 1.44}]}

Two things needed care. The lead time is in weeks while the other two sources report
days, so the unit is passed to the parser rather than assumed. And the orderable facts —
SKU, packaging, MOQ, pricing — live on a variation, not on the product: a product with
tube and tape-and-reel variants has two MOQs and two price ladders, and flattening them
would quote a buyer a price for packaging they are not ordering.

Authentication is OAuth 2-legged. The token lives in this process's memory for its ten
minutes and is never written down: a token on disk outlives the reason it was issued.

The sandbox host sits behind a bot challenge that answers 403 to an ordinary client, so
the production host is used. These are read-only product lookups against a documented
quota (the response carries x-ratelimit-remaining); no ordering endpoint is touched.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .base import Offer, PriceBreak, match_status, parse_int, parse_lead_time_days, parse_price

NAME = "digikey"
CLIENT_ID = "SUPPLYAGENT_SUPPLIER_DIGIKEY_CLIENT_ID"
CLIENT_SECRET = "SUPPLYAGENT_SUPPLIER_DIGIKEY_CLIENT_SECRET"
BASE_URL_VAR = "SUPPLYAGENT_SUPPLIER_DIGIKEY_BASE_URL"
DEFAULT_BASE_URL = "https://api.digikey.com"

#: token, expiry — process-local and deliberately not persisted.
_token: tuple[str, float] | None = None
#: Refresh this far before the stated expiry so a call in flight does not straddle it.
_EXPIRY_MARGIN_SECONDS = 30


def configured(environment: dict[str, str]) -> bool:
    return bool(environment.get(CLIENT_ID) and environment.get(CLIENT_SECRET))


def _base(environment: dict[str, str]) -> str:
    return (environment.get(BASE_URL_VAR) or DEFAULT_BASE_URL).rstrip("/")


def _access_token(environment: dict[str, str], client: httpx.Client) -> str:
    global _token
    if _token is not None and _token[1] - _EXPIRY_MARGIN_SECONDS > time.monotonic():
        return _token[0]
    response = client.post(f"{_base(environment)}/v1/oauth2/token",
                           data={"client_id": environment[CLIENT_ID],
                                 "client_secret": environment[CLIENT_SECRET],
                                 "grant_type": "client_credentials"})
    response.raise_for_status()
    payload = response.json()
    _token = (payload["access_token"], time.monotonic() + float(payload.get("expires_in", 600)))
    return _token[0]


def _headers(environment: dict[str, str], client: httpx.Client) -> dict[str, str]:
    return {"Authorization": f"Bearer {_access_token(environment, client)}",
            "X-DIGIKEY-Client-Id": environment[CLIENT_ID],
            "X-DIGIKEY-Locale-Site": environment.get("SUPPLYAGENT_SUPPLIER_REGION", "US"),
            "X-DIGIKEY-Locale-Currency": environment.get("SUPPLYAGENT_SUPPLIER_CURRENCY", "USD")}


def _to_offer(product: dict[str, Any], environment: dict[str, str],
              variation: dict[str, Any] | None = None) -> Offer:
    currency = environment.get("SUPPLYAGENT_SUPPLIER_CURRENCY", "USD")
    variations = product.get("ProductVariations") or []
    chosen = variation or (variations[0] if variations else {})
    breaks = [
        PriceBreak(min_qty=parse_int(tier.get("BreakQuantity")) or 1,
                   unit_price=parse_price(tier.get("UnitPrice")),
                   currency=currency,
                   raw=str(tier.get("UnitPrice")))
        for tier in chosen.get("StandardPricing") or []
    ]
    status = (product.get("ProductStatus") or {}).get("Status")
    lead_weeks = product.get("ManufacturerLeadWeeks")
    # Package-level stock when the variation states it, product-level otherwise: the
    # buyer gets the packaging they ordered, not the sum across packagings.
    stock_raw = chosen.get("QuantityAvailableforPackageType", product.get("QuantityAvailable"))
    return Offer(
        provider=NAME,
        distributor_sku=str(chosen.get("DigiKeyProductNumber")
                            or product.get("ManufacturerProductNumber") or ""),
        mpn=str(product.get("ManufacturerProductNumber") or ""),
        manufacturer=str((product.get("Manufacturer") or {}).get("Name") or ""),
        packaging=str((chosen.get("PackageType") or {}).get("Name") or "") or None,
        currency=currency,
        moq=parse_int(chosen.get("MinimumOrderQuantity")),
        order_multiple=parse_int(chosen.get("StandardPackage")) or 1,
        stock_qty=parse_int(stock_raw),
        lead_time_days=parse_lead_time_days(lead_weeks, unit_hint="weeks"),
        lifecycle_status=status,
        product_url=product.get("ProductUrl"),
        price_breaks=breaks,
        raw={"stock_qty": stock_raw,
             "lead_time_days": None if lead_weeks is None else f"{lead_weeks} weeks",
             "unit_price_at_moq": breaks[0].raw if breaks else None,
             "discontinued": product.get("Discontinued"),
             "end_of_life": product.get("EndOfLife"),
             "product_status": status},
    )


def search(mpn: str, environment: dict[str, str], client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(f"{_base(environment)}/products/v4/search/{mpn}/productdetails",
                          headers=_headers(environment, client))
    if response.status_code == 404:
        return []
    response.raise_for_status()
    product = response.json().get("Product") or {}
    if not product:
        return []
    actual = str(product.get("ManufacturerProductNumber") or "")
    hits = []
    # One hit per orderable variation: tube and tape-and-reel are different SKUs with
    # different minimum orders, and collapsing them hides that choice from the buyer.
    for variation in product.get("ProductVariations") or [{}]:
        hits.append({
            "distributor_sku": str(variation.get("DigiKeyProductNumber") or actual),
            "mpn": actual,
            "manufacturer": str((product.get("Manufacturer") or {}).get("Name") or ""),
            "packaging": str((variation.get("PackageType") or {}).get("Name") or "") or None,
            "match_status": match_status(mpn, actual),
            "_product": product, "_variation": variation})
    return hits


def offer(sku: str, environment: dict[str, str], client: httpx.Client,
          *, hint: dict[str, Any] | None = None) -> Offer | None:
    if hint is not None and hint.get("_product"):
        return _to_offer(hint["_product"], environment, hint.get("_variation"))
    response = client.get(f"{_base(environment)}/products/v4/search/{sku}/productdetails",
                          headers=_headers(environment, client))
    if response.status_code == 404:
        return None
    response.raise_for_status()
    product = response.json().get("Product") or {}
    return _to_offer(product, environment) if product else None
