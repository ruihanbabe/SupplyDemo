"""Persist one row per tool invocation.

Separate from the registry so the registry stays testable without a database, and so the
dependency runs one way: persistence knows about tool records, tools do not know about
PostgreSQL.

tool_call is append-only (0001 grants SELECT/INSERT only), which is what makes it usable
later as the source for a run's call tree (F28).
"""
from __future__ import annotations

import json
import logging
from uuid import uuid4

from sqlalchemy import text

from tools.registry import ToolCallRecord

logger = logging.getLogger("supplyagent.tools.audit")


class PostgresAuditSink:
    def __init__(self, connection) -> None:
        self.connection = connection

    def record(self, entry: ToolCallRecord) -> None:
        if entry.run_id is None:
            # Nothing to attach the row to. Reported rather than invented: a synthetic
            # run id would put an unauditable call into the audit table.
            logger.warning("Tool %s ran without a run_id; not audited", entry.tool_name)
            return
        response = dict(entry.response or {})
        response["attempts"] = entry.attempts
        self.connection.execute(text("""
            INSERT INTO tool_call (tool_call_id, run_id, trace_id, tool_name, request,
                                   response, status, error_code, duration_ms)
            VALUES (:id, :run, :trace, :name, CAST(:request AS JSONB),
                    CAST(:response AS JSONB), :status, :error, :ms)"""),
            {"id": uuid4(), "run": entry.run_id, "trace": entry.trace_id or "",
             "name": entry.tool_name,
             "request": json.dumps(entry.request, ensure_ascii=False, default=str),
             "response": json.dumps(response, ensure_ascii=False, default=str),
             "status": entry.status, "error": entry.error_code, "ms": entry.duration_ms})
