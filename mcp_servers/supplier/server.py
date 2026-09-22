"""A stdio MCP server exposing distributor lookups.

Deliberately a separate process. Two things follow from that and neither is available
in-process: the API credentials live only here, and a supplier integration that
misbehaves cannot reach the application's database. The parent starts it with a filtered
environment (see tools.mcp_client), so what this process can see is an explicit list
rather than whatever the operator happened to export.

No MCP SDK: the protocol is newline-delimited JSON-RPC 2.0 over stdin/stdout, and
writing it out is both smaller than a dependency and the part worth showing.

Per-provider degradation, not all-or-nothing: a distributor whose credential is present
is called for real and its rows are labelled `real`; one without falls back to the
recorded fixture and is labelled `sample`. Mixing the two in one answer is exactly why
the label belongs on each row rather than on the response (BR-09).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import ADAPTERS, BUCKETS  # noqa: E402
from adapters.base import match_status  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures.json"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "supplyagent-supplier", "version": "0.2.0"}
USER_AGENT = "SupplyAgent/0.1 (+local dev)"
HTTP_TIMEOUT = 20.0

TOOLS = [
    {
        "name": "search_supplier_parts",
        "description": ("按 MPN 在一家分销商检索 SKU。返回 match_status："
                        "exact / suffix_differs / ambiguous。非 exact 不得自动采纳。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": sorted(ADAPTERS)},
                "mpn": {"type": "string"},
            },
            "required": ["provider", "mpn"],
        },
    },
    {
        "name": "get_supplier_offer",
        "description": ("按 MPN 或 distributor_sku 取一家分销商的报价：阶梯价、MOQ、"
                        "订购倍数、库存、交期、包装、币种，并附来源原文。"
                        "未知数量返回 null，不得用 0。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": sorted(ADAPTERS)},
                "mpn": {"type": "string"},
                "distributor_sku": {"type": "string"},
            },
            "required": ["provider"],
        },
    },
]


def _environment() -> dict[str, str]:
    return dict(os.environ)


def _http() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a transport failure onto the vocabulary the rest of the system uses.

    401 and 429 are not the same problem and must not arrive as one: the first needs a
    credential, the second needs patience, and retrying the first is pure waste.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "unauthorized", f"凭据被拒绝（HTTP {code}）"
        if code == 429:
            retry_after = exc.response.headers.get("retry-after", "")
            return "rate_limited", f"触发限流（HTTP 429）{retry_after and ' Retry-After: ' + retry_after}"
        if code >= 500:
            return "provider_error", f"分销商服务端错误 HTTP {code}"
        return "bad_request", f"请求被拒绝 HTTP {code}: {exc.response.text[:160]}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", "分销商未在超时内响应"
    if isinstance(exc, httpx.HTTPError):
        return "transport_error", f"网络故障: {exc!s}"[:160]
    return "adapter_error", f"{type(exc).__name__}: {exc!s}"[:160]


# ---------- fixture fallback ----------

def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _fixture_search(provider: str, mpn: str) -> dict[str, Any]:
    catalogue = (_fixture().get("offers") or {}).get(provider, {})
    hits = []
    for sku, record in sorted(catalogue.items()):
        if mpn.upper() not in {value.upper() for value in record.get("covers_mpns", [])}:
            continue
        if record.get("behaviour") == "timeout":
            time.sleep(record.get("delay_seconds", 30))
        if record.get("behaviour") == "not_found":
            continue
        hits.append({"distributor_sku": sku, "manufacturer": record["manufacturer"],
                     "mpn": record["mpn"], "packaging": record["packaging"],
                     "match_status": match_status(mpn, record["mpn"])})
    return {"provider": provider, "queried_mpn": mpn, "results": hits,
            "provenance": "sample",
            "response_status": "ok" if hits else "not_found"}


def _fixture_offer(provider: str, sku: str) -> dict[str, Any]:
    record = (_fixture().get("offers") or {}).get(provider, {}).get(sku)
    if record is None:
        return {"provider": provider, "distributor_sku": sku, "provenance": "sample",
                "response_status": "not_found"}
    behaviour = record.get("behaviour", "ok")
    if behaviour == "timeout":
        time.sleep(record.get("delay_seconds", 30))
    if behaviour == "not_found":
        return {"provider": provider, "distributor_sku": sku, "provenance": "sample",
                "response_status": "not_found"}
    breaks = [{"min_qty": tier["min_qty"], "unit_price": tier["unit_price"],
               "currency": record.get("currency"), "raw": tier["unit_price"]}
              for tier in record.get("price_breaks") or []]
    return {
        "provider": provider, "distributor_sku": sku, "provenance": "sample",
        "mpn": record["mpn"], "manufacturer": record["manufacturer"],
        "packaging": record["packaging"], "currency": record.get("currency"),
        "moq": record["moq"], "order_multiple": record["order_multiple"],
        "stock_qty": record.get("stock_qty"), "lead_time_days": record.get("lead_time_days"),
        "lifecycle_status": None, "product_url": None, "price_breaks": breaks,
        "raw": {"stock_qty": record.get("stock_qty"),
                "lead_time_days": record.get("lead_time_days"),
                "unit_price_at_moq": breaks[0]["raw"] if breaks else None},
        "response_status": "partial" if behaviour == "partial" else "ok",
    }


# ---------- tools ----------

def search_supplier_parts(arguments: dict[str, Any]) -> dict[str, Any]:
    provider = arguments["provider"]
    mpn = arguments["mpn"]
    adapter = ADAPTERS.get(provider)
    environment = _environment()
    if adapter is None or not adapter.configured(environment):
        return _fixture_search(provider, mpn)
    if not BUCKETS[provider].take(timeout=2.0):
        return {"provider": provider, "queried_mpn": mpn, "results": [],
                "provenance": "real", "response_status": "error",
                "error_code": "rate_limited", "message": "本地限流：请求速率超过配额"}
    try:
        with _http() as client:
            hits = adapter.search(mpn, environment, client)
    except Exception as exc:  # noqa: BLE001 - classified, never swallowed
        code, message = _classify(exc)
        return {"provider": provider, "queried_mpn": mpn, "results": [],
                "provenance": "real", "response_status": "error",
                "error_code": code, "message": message}
    # The adapter's private payload stays on this side of the protocol boundary.
    public = [{key: value for key, value in hit.items() if not key.startswith("_")}
              for hit in hits]
    return {"provider": provider, "queried_mpn": mpn, "results": public,
            "provenance": "real",
            "response_status": "ok" if public else "not_found"}


def get_supplier_offer(arguments: dict[str, Any]) -> dict[str, Any]:
    provider = arguments["provider"]
    sku = arguments.get("distributor_sku") or ""
    mpn = arguments.get("mpn") or ""
    adapter = ADAPTERS.get(provider)
    environment = _environment()
    if adapter is None or not adapter.configured(environment):
        # Mirror the live path: given an MPN, the recording resolves the SKU itself, so
        # a caller cannot tell the two apart except by the provenance label.
        if not sku and mpn:
            found = _fixture_search(provider, mpn)
            hits = found.get("results") or []
            if not hits:
                return {"provider": provider, "provenance": "sample",
                        "response_status": "not_found"}
            payload = _fixture_offer(provider, hits[0]["distributor_sku"])
            payload["match_status"] = hits[0]["match_status"]
            return payload
        payload = _fixture_offer(provider, sku)
        if mpn and payload.get("mpn"):
            payload["match_status"] = match_status(mpn, payload["mpn"])
        return payload
    if not BUCKETS[provider].take(timeout=2.0):
        return {"provider": provider, "distributor_sku": sku, "provenance": "real",
                "response_status": "error", "error_code": "rate_limited",
                "message": "本地限流：请求速率超过配额"}
    try:
        with _http() as client:
            # One search answers both questions at every provider, so the quote reuses
            # it rather than spending a second call to learn the same fields.
            hint = None
            if mpn:
                hits = adapter.search(mpn, environment, client)
                hint = next((hit for hit in hits
                             if not sku or hit["distributor_sku"] == sku), None)
                if hint is None and not sku:
                    return {"provider": provider, "provenance": "real",
                            "response_status": "not_found"}
            offer = adapter.offer(sku or (hint or {}).get("distributor_sku", ""),
                                  environment, client, hint=hint)
    except Exception as exc:  # noqa: BLE001
        code, message = _classify(exc)
        return {"provider": provider, "distributor_sku": sku, "provenance": "real",
                "response_status": "error", "error_code": code, "message": message}
    if offer is None:
        return {"provider": provider, "distributor_sku": sku, "provenance": "real",
                "response_status": "not_found"}
    payload = offer.as_dict()
    payload["provenance"] = "real"
    # Carried on the quote so the caller does not have to search a second time just to
    # learn whether the part number matched. Quota is the scarce thing here.
    if hint is not None:
        payload["match_status"] = hint.get("match_status")
    elif mpn:
        payload["match_status"] = match_status(mpn, payload.get("mpn") or "")
    # Stock present but no usable price is partial, not ok: a buyer cannot act on it.
    if payload["response_status"] == "ok" and not payload.get("price_breaks"):
        payload["response_status"] = "partial"
    return payload


HANDLERS = {"search_supplier_parts": search_supplier_parts,
            "get_supplier_offer": get_supplier_offer}


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO}}
    if method == "notifications/initialized":
        return None  # A notification has no id and takes no reply.
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        handler = HANDLERS.get(params.get("name"))
        if handler is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601,
                              "message": f"Unknown tool {params.get('name')!r}"}}
        try:
            payload = handler(params.get("arguments") or {})
        except Exception as exc:  # noqa: BLE001 - a tool fault is reported, not fatal
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": str(exc)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False)}]}}
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Unknown method {method!r}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
