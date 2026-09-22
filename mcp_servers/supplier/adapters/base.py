"""Shared shape and helpers for distributor adapters.

Three distributors answer the same question in three different ways: stock arrives as
`'0'`, as `{"level": 2802}` and as `6484`; lead time as `'224 Days'`, as `337` and in
weeks; prices in RMB, GBP and USD. Normalising is unavoidable — but the source's own
wording is kept alongside, because a conclusion has to be checkable against what the
supplier actually said, not against our reading of it.

Normalisation that fails leaves the normalised field empty. A plausible number guessed
from an unparseable string is worse than no number.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

#: Units a distributor might state a lead time in. Anything else stays unparsed.
_LEAD_TIME = re.compile(r"(\d+(?:\.\d+)?)\s*(day|days|week|weeks|wk|wks)?", re.IGNORECASE)
_WEEK_UNITS = {"week", "weeks", "wk", "wks"}
#: A price string may carry a symbol, thousands separators, or a trailing code.
_PRICE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


@dataclass
class PriceBreak:
    min_qty: int
    unit_price: str | None
    currency: str | None
    #: Exactly what the source printed, e.g. "¥10.54".
    raw: str | None = None


@dataclass
class Offer:
    """One distributor's answer about one part, normalised but not flattened."""

    provider: str
    distributor_sku: str
    mpn: str
    manufacturer: str
    response_status: str = "ok"
    packaging: str | None = None
    currency: str | None = None
    moq: int | None = None
    order_multiple: int | None = None
    stock_qty: int | None = None
    lead_time_days: int | None = None
    lifecycle_status: str | None = None
    product_url: str | None = None
    price_breaks: list[PriceBreak] = field(default_factory=list)
    #: The source's own rendering of each fact, keyed by the same names as above. This is
    #: what lands in evidence.value_raw; the normalised value lands in value_normalized,
    #: and where parsing failed the latter is null rather than invented.
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_int(value: Any) -> int | None:
    """Digits only. '1,234 In Stock' is 1234; 'Call' is unknown, not zero."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"\d[\d,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def parse_lead_time_days(value: Any, *, unit_hint: str | None = None) -> int | None:
    """Days, from whatever the source said.

    `unit_hint` covers sources that publish a bare number in a field whose name carries
    the unit — DigiKey's ManufacturerLeadWeeks being the case that forced it.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        days = int(value)
        return days * 7 if unit_hint in _WEEK_UNITS else days
    match = _LEAD_TIME.search(str(value))
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or unit_hint or "day").lower()
    return int(amount * 7) if unit in _WEEK_UNITS else int(amount)


def parse_price(value: Any) -> str | None:
    """The number out of a price string, as a string.

    Kept as text all the way through: these become NUMERIC columns downstream, and
    routing money through a float on the way there is exactly what BR-02 forbids.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return f"{value}"
    match = _PRICE.search(str(value))
    return match.group(1).replace(",", "") if match else None


def match_status(queried: str, actual: str) -> str:
    """How close the distributor's part number is to what was asked for.

    Case-only differences are `suffix_differs`, not `exact`: whether IRFZ44NPbF and
    IRFZ44NPBF are the same orderable part is a question for a person (EV-04). Anything
    further apart is `ambiguous`, and neither is ever adopted automatically.
    """
    if queried == actual:
        return "exact"
    if queried.upper() == actual.upper():
        return "suffix_differs"
    return "ambiguous"
