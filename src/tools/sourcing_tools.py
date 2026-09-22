"""Multi-source distributor lookups, reached over MCP.

One client per provider, not one shared client. A supplier that hangs gets its own
connection killed; the other two carry on. Sharing a process would make one slow
distributor look like an outage of all of them.

Divergence is preserved, never merged. Three distributors reporting 900, 150 and 0 in
stock are three observations, and the average of them is a number no one can act on
(BR-06). The comparison shows all three and says so.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from contracts.evidence import FieldLocator, Observation
from contracts.llm import ToolSpec
from persistence.evidence import EvidenceLedger
from tools.mcp_client import SUPPLIER_SERVER, MCPClient, MCPError
from tools.registry import RegisteredTool, RetryPolicy, ToolOutcome

logger = logging.getLogger("supplyagent.sourcing")

PROVIDERS = ("digikey", "mouser", "element14")

#: Which response fields become Evidence, and under which evidence kind. Only facts the
#: distributor asserted — nothing derived, because derived numbers belong to the
#: deterministic core and would be untraceable if filed as observations.
EVIDENCE_FIELDS = (
    ("stock", "stock_qty"),
    ("lead_time", "lead_time_days"),
    ("price", "unit_price_at_moq"),
)


def _digest(provider: str, method: str, arguments: dict[str, Any]) -> str:
    """Reproduce-this-query key. Arguments only — no credentials ever enter it."""
    blob = json.dumps({"provider": provider, "method": method, "arguments": arguments},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _price_at_moq(breaks: list[dict[str, Any]], moq: Any) -> dict[str, Any] | None:
    """The tier a buyer lands in at this distributor's minimum order.

    Picked rather than computed: it is one of the tiers the distributor published, so it
    stays an observation. Interpolating between tiers would be arithmetic, and arithmetic
    belongs to the procurement core where it can be checked.
    """
    if not breaks:
        return None
    floor = float(moq or 1)
    applicable = [tier for tier in breaks if float(tier.get("min_qty") or 1) <= floor]
    chosen = (max(applicable, key=lambda tier: float(tier["min_qty"]))
              if applicable else breaks[0])
    return {"unit_price": chosen.get("unit_price"), "currency": chosen.get("currency"),
            "raw": chosen.get("raw")}


def _query_provider(provider: str, mpn: str, spec=SUPPLIER_SERVER) -> dict[str, Any]:
    """One call per provider, on its own connection.

    The server does the search and the quote together because every provider answers
    both from one response; asking twice would spend quota to learn the same fields.

    Failures are classified, not swallowed: a timeout and an empty catalogue lead to
    different purchasing decisions, so they come back as different statuses (BR-05).

    `spec` is injectable so a test can shorten the timeout it is asserting about; what is
    under test is the classification, not how long the wait was.
    """
    arguments = {"provider": provider, "mpn": mpn}
    try:
        with MCPClient(spec) as client:
            offer = client.call_tool("get_supplier_offer", arguments)
    except MCPError as exc:
        return {"provider": provider, "status": "error", "error_code": exc.code,
                "provenance": "unknown",
                "request_digest": _digest(provider, "get_supplier_offer", arguments),
                "message": str(exc)[:160]}

    digest = _digest(provider, "get_supplier_offer", arguments)
    # Per row, not per response: with one provider answering for real and another falling
    # back to a recording, a single label on the answer would be false for half of it.
    provenance = offer.get("provenance", "unknown")
    status = offer.get("response_status", "error")
    if status == "not_found":
        return {"provider": provider, "status": "not_found", "provenance": provenance,
                "request_digest": digest, "message": "该分销商目录中没有这个型号"}
    if status == "error":
        return {"provider": provider, "status": "error", "provenance": provenance,
                "error_code": offer.get("error_code") or "provider_error",
                "request_digest": digest,
                "message": offer.get("message") or "分销商返回错误"}

    breaks = offer.get("price_breaks") or []
    at_moq = _price_at_moq(breaks, offer.get("moq"))
    return {
        "provider": provider,
        "status": "partial" if status == "partial" else "ok",
        "provenance": provenance,
        "request_digest": digest,
        "distributor_sku": offer.get("distributor_sku"),
        "distributor_mpn": offer.get("mpn"),
        # Never silently adopted: an inexact match is a question for a person, and the
        # answer changes which part gets ordered (EV-04).
        "match_status": offer.get("match_status"),
        "packaging": offer.get("packaging"),
        "currency": offer.get("currency"),
        "moq": offer.get("moq"),
        "order_multiple": offer.get("order_multiple"),
        "stock_qty": offer.get("stock_qty"),
        "lead_time_days": offer.get("lead_time_days"),
        "lifecycle_status": offer.get("lifecycle_status"),
        "unit_price_at_moq": at_moq["unit_price"] if at_moq else None,
        # Price without currency is not a comparable number (BR-03), so the two always
        # travel together and neither is ever shown without the other.
        "price_currency": at_moq["currency"] if at_moq else None,
        # What the source itself printed, kept for evidence.value_raw.
        "raw": offer.get("raw") or {},
    }


def _record_evidence(ledger: EvidenceLedger, run_id, mpn: str, row: dict[str, Any]) -> list[str]:
    """File what each distributor said, one observation per fact.

    Per field rather than per response: a conclusion cites the lead time, not "the Mouser
    reply", and a citation pointing at a whole payload cannot be checked.

    value_raw is the source's own rendering — '224 Days', '¥10.54', '0'. The parsed
    number goes in value_normalized, and where parsing failed that stays empty rather
    than carrying a guess. Keeping both is what lets someone check our reading against
    what the supplier actually wrote.
    """
    raw = row.get("raw") or {}
    recorded = []
    for kind, field in EVIDENCE_FIELDS:
        normalized = row.get(field)
        original = raw.get(field, normalized)
        if normalized is None and original is None:
            # Unknown is not zero and not an observation. Filing it would make an absent
            # answer indistinguishable from a reported one.
            continue
        attribute = field
        if field == "unit_price_at_moq" and row.get("price_currency"):
            # Currency is part of the fact, not a footnote: 1.32 and 1.44 are not
            # comparable when one is GBP and the other USD (BR-03).
            attribute = f"{field}[{row['price_currency']}]"
        evidence_id, _ = ledger.record(Observation(
            run_id=run_id, kind=kind, subject_ref=mpn, attribute=attribute,
            value_raw=str(original if original is not None else normalized),
            value_normalized=None if normalized is None else str(normalized),
            retrieved_at=datetime.now(UTC),
            provenance=row.get("provenance") if row.get("provenance") in
            ("real", "cache", "sample", "replay") else "sample",
            locator=FieldLocator(source_name=row["provider"],
                                 request_digest=row["request_digest"],
                                 field_path=field,
                                 response_status="partial" if row["status"] == "partial"
                                 else "ok")))
        recorded.append(str(evidence_id))
    return recorded


def compare_supplier_offers(arguments: dict[str, Any], context) -> ToolOutcome:
    mpn = str(arguments["mpn"])
    rows = [_query_provider(provider, mpn) for provider in PROVIDERS]

    ledger = EvidenceLedger(context.repository.connection)
    for row in rows:
        if row["status"] in {"ok", "partial"} and context.run_id is not None:
            row["evidence_ids"] = _record_evidence(ledger, context.run_id, mpn, row)

    usable = [row for row in rows if row["status"] in {"ok", "partial"}]
    distinct_stock = {row.get("stock_qty") for row in usable if row.get("stock_qty") is not None}
    currencies = {row.get("price_currency") for row in usable if row.get("price_currency")}
    content = {
        "mpn": mpn,
        "providers_queried": list(PROVIDERS),
        "rows": rows,
        # Stated rather than resolved. Which number is right is a question about the
        # world; this layer only reports that the sources disagree (BR-06).
        "stock_disagreement": len(distinct_stock) > 1,
        # Prices in different currencies are not comparable without a verified rate, and
        # this system does not have one. Saying so beats letting someone read the column
        # downwards (BR-03).
        "currencies": sorted(currencies),
        "prices_comparable": len(currencies) <= 1,
        "inexact_matches": sorted({row["provider"] for row in usable
                                   if row.get("match_status") not in (None, "exact")}),
        "provenance_by_provider": {row["provider"]: row.get("provenance") for row in rows},
    }
    failures = [row["provider"] for row in rows if row["status"] != "ok"]
    if not usable:
        return ToolOutcome("error", render="offers", content=content,
                           error_code="all_sources_failed",
                           message="三家分销商都没有可用结果")
    notes = []
    if failures:
        notes.append(f"{len(failures)} 家未给出完整结果：{'、'.join(failures)}")
    if not content["prices_comparable"]:
        notes.append(f"价格币种不一致（{'、'.join(content['currencies'])}），不可直接比较")
    # The tool's own provenance is the weakest of its rows: one recorded answer among
    # live ones makes the whole comparison partly simulated.
    provenance = "real" if all(row.get("provenance") == "real" for row in usable) else "sample"
    return ToolOutcome("ok" if not failures else "partial", render="offers",
                       content=content, message="；".join(notes) or None,
                       provenance=provenance)


SOURCING_TOOLS = (
    RegisteredTool(
        spec=ToolSpec(
            name="compare_supplier_offers",
            description=("按 MPN 并查 digikey / mouser / farnell 三家分销商的库存、交期与"
                         "阶梯价。各源结果独立呈现，分歧不合并；非 exact 的型号匹配会标出，"
                         "不自动采纳。当前为模拟数据。"),
            parameters={"type": "object",
                        "properties": {"mpn": {"type": "string",
                                               "description": "完整制造商型号"}},
                        "required": ["mpn"]}),
        effect="read", handler=compare_supplier_offers,
        permission="sourcing.read", idempotency="idempotent",
        # Room for three providers, one of which is scripted to hang. The registry does
        # not retry: a distributor that timed out will time out again, and the honest
        # answer is that this source is unavailable right now.
        timeout_seconds=30.0, retry_policy=RetryPolicy(attempts=1),
        scenarios=frozenset({"procurement"})),
)
