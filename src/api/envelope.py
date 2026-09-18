"""HTTP response envelope, shaped like the ToolResult contract in docs/spec/interfaces.md.

Numbers leave this layer as strings: BR-03 forbids implicit rounding, and JSON
numbers are IEEE-754 doubles in every mainstream client parser.
"""
from __future__ import annotations

from typing import Any

from procurement_core.shortage import json_value

SCHEMA_VERSION = 1

#: Business outcomes, not transport failures. An API error is not the same thing as
#: a business miss: "no supply found" is ok/partial with empty data, never an error.
STATUSES = ("ok", "partial", "not_found", "error")


def envelope(
    data: Any = None,
    *,
    status: str = "ok",
    warnings: tuple[str, ...] | list[str] = (),
    error: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"Unknown envelope status: {status}")
    if (status == "error") != (error is not None):
        raise ValueError("Envelope status 'error' and the error object must agree")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "data": json_value(data),
        "warnings": list(warnings),
        "error": error,
        "trace_id": trace_id,
    }


def error_envelope(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return envelope(
        None,
        status="error",
        error={"code": code, "message": message, "retryable": retryable},
        trace_id=trace_id,
    )
