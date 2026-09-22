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

PROVIDERS = ("digikey", "mouser", "farnell")

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


def _unit_price_at_moq(offer: dict[str, Any]) -> str | None:
    """The price a buyer would actually pay at this distributor's minimum order.

    Picked rather than computed: it is one of the tiers the distributor published, so it
    stays an observation. Interpolating between tiers would be arithmetic, and arithmetic
    belongs to the procurement core.
    """
    breaks = offer.get("price_breaks") or []
    if not breaks:
        return None
    moq = offer.get("moq") or 1
    applicable = [tier for tier in breaks if float(tier["min_qty"]) <= float(moq)]
    chosen = max(applicable, key=lambda tier: float(tier["min_qty"])) if applicable else breaks[0]
    return str(chosen["unit_price"])


def _query_provider(provider: str, mpn: str, spec=SUPPLIER_SERVER) -> dict[str, Any]:
    """Search then quote, on this provider's own connection.

    Failures are classified, not swallowed: a timeout and an empty catalogue lead to
    different purchasing decisions, so they come back as different statuses (BR-05).

    `spec` is injectable so a test can shorten the timeout it is asserting about; what is
    under test is the classification, not how long the wait was.
    """
    arguments = {"provider": provider, "mpn": mpn}
    try:
        with MCPClient(spec) as client:
            found = client.call_tool("search_supplier_parts", arguments)
            hits = found.get("results") or []
            if not hits:
                return {"provider": provider, "status": "not_found",
                        "request_digest": _digest(provider, "search_supplier_parts", arguments),
                        "message": "该分销商目录中没有这个型号"}
            hit = hits[0]
            offer_arguments = {"provider": provider,
                               "distributor_sku": hit["distributor_sku"]}
            offer = client.call_tool("get_supplier_offer", offer_arguments)
    except MCPError as exc:
        return {"provider": provider, "status": "error", "error_code": exc.code,
                "request_digest": _digest(provider, "search_supplier_parts", arguments),
                "message": str(exc)[:160]}
    if offer.get("response_status") == "not_found":
        return {"provider": provider, "status": "not_found",
                "request_digest": _digest(provider, "get_supplier_offer", offer_arguments),
                "message": "SKU 存在于目录但取不到报价"}
    return {
        "provider": provider,
        "status": "partial" if offer.get("response_status") == "partial" else "ok",
        "request_digest": _digest(provider, "get_supplier_offer", offer_arguments),
        "distributor_sku": hit["distributor_sku"],
        "distributor_mpn": hit["mpn"],
        # Never silently adopted: an inexact match is a question for a person, and the
        # answer changes which part gets ordered (EV-04).
        "match_status": hit["match_status"],
        "packaging": offer.get("packaging"),
        "currency": offer.get("currency"),
        "moq": offer.get("moq"),
        "order_multiple": offer.get("order_multiple"),
        "stock_qty": offer.get("stock_qty"),
        "lead_time_days": offer.get("lead_time_days"),
        "unit_price_at_moq": _unit_price_at_moq(offer),
    }


def _record_evidence(ledger: EvidenceLedger, run_id, mpn: str, row: dict[str, Any]) -> list[str]:
    """File what each distributor said, one observation per fact.

    Per field rather than per response: a conclusion cites the lead time, not "the
    Mouser reply", and a citation that points at a whole payload cannot be checked.
    """
    recorded = []
    for kind, field in EVIDENCE_FIELDS:
        value = row.get(field)
        if value is None:
            # Unknown is not zero and not an observation. Filing it would make an
            # absent answer indistinguishable from a reported one.
            continue
        evidence_id, _ = ledger.record(Observation(
            run_id=run_id, kind=kind, subject_ref=mpn, attribute=field,
            value_raw=str(value), value_normalized=str(value),
            retrieved_at=datetime.now(UTC), provenance="sample",
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
    stocks = {row["provider"]: row.get("stock_qty") for row in usable}
    distinct = {value for value in stocks.values() if value is not None}
    content = {
        "mpn": mpn,
        "providers_queried": list(PROVIDERS),
        "rows": rows,
        # Stated rather than resolved. Which number is right is a question about the
        # world, and this layer only reports that the sources disagree.
        "stock_disagreement": len(distinct) > 1,
        "inexact_matches": sorted({row["provider"] for row in usable
                                   if row.get("match_status") not in (None, "exact")}),
    }
    failures = [row["provider"] for row in rows if row["status"] not in {"ok"}]
    if not usable:
        return ToolOutcome("error", render="offers", content=content,
                           error_code="all_sources_failed", provenance="sample",
                           message="三家分销商都没有可用结果")
    status = "ok" if not failures else "partial"
    message = None if not failures else f"{len(failures)} 家未给出完整结果：{'、'.join(failures)}"
    return ToolOutcome(status, render="offers", content=content, message=message,
                       provenance="sample")


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
