"""F42 Python Harness 与真实 Node JSONL fixture 的离线契约。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from product_api.harness import (
    Harness,
    HarnessContext,
    HarnessProtocolError,
    MemoryTraceSink,
    PiRuntimeAdapter,
    ToolDefinition,
    ToolRegistry,
)

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "pi_sidecar" / "f42_protocol_sidecar.mjs"


def _context() -> HarnessContext:
    return HarnessContext(uuid4(), uuid4(), None, "调查 PART-A 的确定性缺料影响", ("supply:PART-A",))


def _tools() -> ToolRegistry:
    return ToolRegistry((ToolDefinition("supply.impact", lambda arguments: {"part": arguments["partNumber"]}),))


def test_real_node_sidecar_round_trip_uses_only_python_registered_tool() -> None:
    async def scenario() -> None:
        trace = MemoryTraceSink()
        result = await Harness(PiRuntimeAdapter(("node", str(SIDECAR))), _tools()).execute(_context(), trace)
        assert result.status == "completed"
        assert result.tool_calls == ("supply.impact",)
        assert [item["eventType"] for item in trace.events] == [
            "pi.turn_started",
            "pi.tool_call",
            "pi.turn_completed",
        ]
        assert trace.events[1]["metadata"] == {"callId": "impact-1", "name": "supply.impact"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "line",
    [
        '{"protocolVersion":1,"type":"turn.completed","turnId":"wrong","status":"completed"}',
        '{"protocolVersion":1,"type":"turn.started","turnId":"TURN","token":"leak"}',
        '{"protocolVersion":2,"type":"turn.started","turnId":"TURN"}',
    ],
)
def test_protocol_rejects_wrong_turn_secret_field_and_unknown_version(line: str) -> None:
    async def scenario() -> None:
        program = f"import sys; print({line!r}); sys.stdout.flush()"
        adapter = PiRuntimeAdapter((sys.executable, "-c", program))
        with pytest.raises(HarnessProtocolError):
            await adapter.run_turn(_context(), _tools(), MemoryTraceSink())

    asyncio.run(scenario())


def test_registry_rejects_any_non_read_only_f42_tool_or_extra_arguments() -> None:
    with pytest.raises(ValueError):
        ToolRegistry((ToolDefinition("shell.exec", lambda _arguments: {}),))

    async def scenario() -> None:
        with pytest.raises(Exception, match="固定 schema"):
            await _tools().execute("supply.impact", {"partNumber": "PART-A", "shell": "rm"})

    asyncio.run(scenario())


def test_context_does_not_accept_unbounded_task_or_fact_reference() -> None:
    with pytest.raises(ValueError):
        HarnessContext(uuid4(), uuid4(), None, "x" * 1_001)
    with pytest.raises(ValueError):
        HarnessContext(uuid4(), uuid4(), None, "task", ("x" * 161,))
