"""A stdio MCP server exposing distributor lookups.

Deliberately a separate process. Two things follow from that and neither is available
in-process: the API credential lives only here, and a supplier integration that
misbehaves cannot reach the application's database. The parent starts it with a filtered
environment (see tools.mcp_client), so what this process can see is an explicit list
rather than whatever the operator happened to export.

No MCP SDK: the protocol is newline-delimited JSON-RPC 2.0 over stdin/stdout, and
writing it out is both smaller than a dependency and the part worth showing.

The data source is a fixture today. Real credentials are not settled (Q-01), and the
protocol demonstration does not need them — when they arrive, only `_load` changes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures.json"

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "supplyagent-supplier", "version": "0.1.0"}

TOOLS = [
    {
        "name": "search_supplier_parts",
        "description": ("按 MPN 在一家分销商检索 SKU。返回 match_status："
                        "exact / suffix_differs / ambiguous。非 exact 不得自动采纳。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["digikey", "mouser", "farnell"]},
                "mpn": {"type": "string"},
                "region": {"type": "string", "default": "US"},
            },
            "required": ["provider", "mpn"],
        },
    },
    {
        "name": "get_supplier_offer",
        "description": ("按已核验的 distributor_sku 取报价：阶梯价、MOQ、订购倍数、"
                        "库存、交期、包装、币种。未知数量返回 null，不得用 0。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["digikey", "mouser", "farnell"]},
                "distributor_sku": {"type": "string"},
                "region": {"type": "string", "default": "US"},
                "currency": {"type": "string", "default": "USD"},
            },
            "required": ["provider", "distributor_sku"],
        },
    },
]


def _load() -> dict[str, Any]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _match_status(queried: str, actual: str) -> str:
    """How close the distributor's part number is to what was asked for.

    Case-only differences are reported as `suffix_differs`, not `exact`: the BOM says
    IRFZ44NPbF and the distributor says IRFZ44NPBF, and whether those are the same
    orderable part is a question for a person, not a string comparison. Anything further
    apart is `ambiguous` — one distributor SKU genuinely maps to two different
    manufacturer part numbers in this data, and picking one would be inventing an
    identity the source never asserted.
    """
    if queried == actual:
        return "exact"
    if queried.upper() == actual.upper():
        return "suffix_differs"
    return "ambiguous"


def search_supplier_parts(arguments: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    provider = arguments["provider"]
    mpn = arguments["mpn"]
    catalogue = data["offers"].get(provider, {})
    hits = []
    for sku, record in sorted(catalogue.items()):
        if mpn.upper() not in {value.upper() for value in record.get("covers_mpns", [])}:
            continue
        behaviour = record.get("behaviour", "ok")
        if behaviour == "timeout":
            time.sleep(record.get("delay_seconds", 30))
        if behaviour == "not_found":
            continue
        hits.append({
            "distributor_sku": sku,
            "manufacturer": record["manufacturer"],
            "mpn": record["mpn"],
            "packaging": record["packaging"],
            "match_status": _match_status(mpn, record["mpn"]),
        })
    return {"provider": provider, "queried_mpn": mpn, "results": hits,
            "response_status": "ok" if hits else "not_found"}


def get_supplier_offer(arguments: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    provider = arguments["provider"]
    sku = arguments["distributor_sku"]
    record = data["offers"].get(provider, {}).get(sku)
    if record is None:
        return {"provider": provider, "distributor_sku": sku, "response_status": "not_found"}
    behaviour = record.get("behaviour", "ok")
    if behaviour == "timeout":
        time.sleep(record.get("delay_seconds", 30))
    if behaviour == "not_found":
        return {"provider": provider, "distributor_sku": sku, "response_status": "not_found"}
    offer = {
        "provider": provider,
        "distributor_sku": sku,
        "manufacturer": record["manufacturer"],
        "mpn": record["mpn"],
        "packaging": record["packaging"],
        "region": arguments.get("region", "US"),
        # Unknown stays null. A zero here would read as "none in stock", which is a
        # different and actionable fact.
        "stock_qty": record.get("stock_qty"),
        "lead_time_days": record.get("lead_time_days"),
        "moq": record["moq"],
        "order_multiple": record["order_multiple"],
        "currency": record.get("currency"),
        "price_breaks": record.get("price_breaks") or [],
        "response_status": "partial" if behaviour == "partial" else "ok",
    }
    return offer


HANDLERS = {"search_supplier_parts": search_supplier_parts,
            "get_supplier_offer": get_supplier_offer}


def handle(message: dict[str, Any], data: dict[str, Any]) -> dict[str, Any] | None:
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
                    "error": {"code": -32601, "message": f"Unknown tool {params.get('name')!r}"}}
        try:
            payload = handler(params.get("arguments") or {}, data)
        except Exception as exc:  # noqa: BLE001 - a tool fault is reported, not fatal
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": str(exc)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False)}]}}
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Unknown method {method!r}"}}


def main() -> int:
    data = _load()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(message, data)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
