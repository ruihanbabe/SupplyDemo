"""element14 / Farnell / Newark Product Search API.

Read off a live response for IRFZ44NPBF against uk.farnell.com:

    sku                               '8650225'
    translatedManufacturerPartNumber  'IRFZ44NPBF'
    brandName                         'INFINEON'            (upper case)
    stock                             {'level': 2802, 'leastLeadTime': 337, ...}
    translatedMinimumOrderQuality     1
    packSize                          1
    prices[0]                         {'from': 1, 'to': 9, 'cost': 1.32}

Two shapes here differ from every other source. Stock is nested rather than scalar, and
prices are ranges (`from`/`to`) rather than break quantities — the lower bound is the
break, and reading `to` as the break would put the buyer in the wrong tier.

The currency is decided by the store, not by a parameter: uk.farnell.com quotes GBP,
newark.com quotes USD. It is therefore derived from the configured store rather than
assumed, because an unlabelled 1.32 next to a 1.44 invites a comparison that is wrong.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Offer, PriceBreak, match_status, parse_int, parse_lead_time_days, parse_price

NAME = "element14"
BASE_URL = "https://api.element14.com/catalog/products"
CREDENTIAL = "SUPPLYAGENT_SUPPLIER_ELEMENT14_API_KEY"
STORE = "SUPPLYAGENT_SUPPLIER_ELEMENT14_STORE"

#: Which currency a store quotes in. Unlisted stores yield None rather than a guess: an
#: unlabelled amount must not enter a comparison (BR-03).
STORE_CURRENCY = {
    "uk.farnell.com": "GBP",
    "www.newark.com": "USD",
    "canada.newark.com": "CAD",
    "sg.element14.com": "SGD",
    "au.element14.com": "AUD",
    "cn.element14.com": "CNY",
    "de.farnell.com": "EUR",
    "fr.farnell.com": "EUR",
}


def configured(environment: dict[str, str]) -> bool:
    return bool(environment.get(CREDENTIAL))


def _store(environment: dict[str, str]) -> str:
    return environment.get(STORE) or "uk.farnell.com"


def _to_offer(product: dict[str, Any], environment: dict[str, str]) -> Offer:
    currency = STORE_CURRENCY.get(_store(environment))
    stock = product.get("stock") or {}
    breaks = []
    for tier in product.get("prices") or []:
        # `from` is the break quantity. `to` is where the next tier starts.
        breaks.append(PriceBreak(min_qty=parse_int(tier.get("from")) or 1,
                                 unit_price=parse_price(tier.get("cost")),
                                 currency=currency,
                                 raw=str(tier.get("cost"))))
    return Offer(
        provider=NAME,
        distributor_sku=str(product.get("sku") or ""),
        mpn=str(product.get("translatedManufacturerPartNumber") or ""),
        manufacturer=str(product.get("brandName") or ""),
        packaging=str(product.get("unitOfMeasure") or "") or None,
        currency=currency,
        moq=parse_int(product.get("translatedMinimumOrderQuality")),
        order_multiple=parse_int(product.get("packSize")),
        stock_qty=parse_int(stock.get("level")),
        lead_time_days=parse_lead_time_days(stock.get("leastLeadTime")),
        lifecycle_status=str(product.get("productStatus") or "") or None,
        product_url=None,
        price_breaks=breaks,
        raw={"stock_qty": stock.get("level"),
             "lead_time_days": stock.get("leastLeadTime"),
             "inventory_code": product.get("inventoryCode"),
             "unit_price_at_moq": breaks[0].raw if breaks else None,
             "store": _store(environment)},
    )


def _products(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("manufacturerPartNumberSearchReturn", "premierFarnellPartNumberReturn",
                "keywordSearchReturn"):
        body = payload.get(key)
        if body:
            return body.get("products") or []
    return []


def search(mpn: str, environment: dict[str, str], client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(BASE_URL, params={
        "term": f"manuPartNum:{mpn}",
        "storeInfo.id": _store(environment),
        "resultsSettings.offset": 0,
        "resultsSettings.numberOfResults": 5,
        "resultsSettings.responseGroup": "large",
        "callInfo.responseDataFormat": "json",
        "callInfo.apiKey": environment[CREDENTIAL]})
    response.raise_for_status()
    hits = []
    for product in _products(response.json()):
        actual = str(product.get("translatedManufacturerPartNumber") or "")
        hits.append({"distributor_sku": str(product.get("sku") or ""),
                     "mpn": actual,
                     "manufacturer": str(product.get("brandName") or ""),
                     "packaging": str(product.get("unitOfMeasure") or "") or None,
                     "match_status": match_status(mpn, actual),
                     "_product": product})
    return hits


def offer(sku: str, environment: dict[str, str], client: httpx.Client,
          *, hint: dict[str, Any] | None = None) -> Offer | None:
    """The search response already carries prices and stock, so reuse it when present."""
    if hint is not None and hint.get("_product"):
        return _to_offer(hint["_product"], environment)
    response = client.get(BASE_URL, params={
        "term": f"id:{sku}", "storeInfo.id": _store(environment),
        "resultsSettings.offset": 0, "resultsSettings.numberOfResults": 1,
        "resultsSettings.responseGroup": "large",
        "callInfo.responseDataFormat": "json",
        "callInfo.apiKey": environment[CREDENTIAL]})
    response.raise_for_status()
    products = _products(response.json())
    return _to_offer(products[0], environment) if products else None
