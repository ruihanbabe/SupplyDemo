"""F42 Python Harness 与真实 Node JSONL fixture 的离线契约。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from product_api.harness import (
    PI_CORE_ENGINE,
    Harness,
    HarnessContext,
    HarnessProtocolError,
    HarnessToolError,
    HarnessTurnError,
    MemoryTraceSink,
    PiRuntimeAdapter,
    ToolDefinition,
    ToolRegistry,
)

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "pi_sidecar" / "f42_protocol_sidecar.mjs"
MINIMAL_ENGINE = ROOT / "tests" / "fixtures" / "f42_minimal_agent_engine.py"


def _context() -> HarnessContext:
    return HarnessContext(uuid4(), uuid4(), None, "调查 PART-A 的确定性缺料影响", ("supply:PART-A",))


def _tools() -> ToolRegistry:
    return ToolRegistry((ToolDefinition("supply.impact", lambda arguments: {"part": arguments["partNumber"]}),))


def test_pinned_pi_core_replay_round_trip_uses_only_python_registered_tool() -> None:
    async def scenario() -> None:
        trace = MemoryTraceSink()
        result = await Harness(PiRuntimeAdapter(("node", str(SIDECAR))), _tools()).execute(_context(), trace)
        assert result.status == "completed"
        assert result.tool_calls == ("supply.impact", "supply.impact")
        assert result.output is not None
        assert result.output["role"] == "supply_risk"
        assert [item["eventType"] for item in trace.events] == [
            "pi.turn_started",
            "pi.tool_call",
            "pi.tool_call",
            "pi.model_delta",
            "pi.turn_completed",
        ]
        assert trace.events[0]["metadata"] == {"engine": PI_CORE_ENGINE}
        assert trace.events[1]["metadata"] == {"callId": "impact-1", "name": "supply.impact"}
        assert trace.events[2]["metadata"] == {"callId": "impact-2", "name": "supply.impact"}
        assert trace.events[3]["metadata"]["length"] > 0

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


def test_protocol_rejects_non_object_structured_output() -> None:
    async def scenario() -> None:
        program = (
            "import json,sys; start=json.loads(input()); "
            "print(json.dumps({'protocolVersion':1,'type':'turn.started','turnId':start['turnId']})); "
            "print(json.dumps({'protocolVersion':1,'type':'turn.completed','turnId':start['turnId'],"
            "'status':'completed','output':[]})); sys.stdout.flush()"
        )
        with pytest.raises(HarnessProtocolError, match="output"):
            await PiRuntimeAdapter((sys.executable, "-c", program)).run_turn(
                _context(), _tools(), MemoryTraceSink()
            )

    asyncio.run(scenario())


def test_registry_rejects_any_non_read_only_f42_tool_or_extra_arguments() -> None:
    with pytest.raises(ValueError):
        ToolRegistry((ToolDefinition("shell.exec", lambda _arguments: {}, risk_level="r2"),))
    with pytest.raises(ValueError, match="参数校验器"):
        ToolRegistry((ToolDefinition("knowledge.search", lambda _arguments: {}),))

    async def scenario() -> None:
        with pytest.raises(Exception, match="固定 schema"):
            await _tools().execute("supply.impact", {"partNumber": "PART-A", "shell": "rm"})

    asyncio.run(scenario())


def test_context_does_not_accept_unbounded_task_or_fact_reference() -> None:
    with pytest.raises(ValueError):
        HarnessContext(uuid4(), uuid4(), None, "x" * 1_001)
    with pytest.raises(ValueError):
        HarnessContext(uuid4(), uuid4(), None, "task", ("x" * 161,))


@pytest.mark.parametrize(
    ("program", "code"),
    [
        (
            "import json,sys; start=json.loads(input()); print(json.dumps({'protocolVersion':1,'type':'turn.started','turnId':start['turnId']})); print(json.dumps({'protocolVersion':1,'type':'turn.failed','turnId':start['turnId'],'code':'model_failed'})); sys.stdout.flush()",
            "model_failed",
        ),
        (
            "import json,sys; start=json.loads(input()); print(json.dumps({'protocolVersion':1,'type':'turn.started','turnId':start['turnId']})); sys.stdout.flush()",
            "sidecar_exited",
        ),
        ("import time; time.sleep(1)", "timeout"),
    ],
)
def test_turn_failures_are_stable_and_traced(program: str, code: str) -> None:
    async def scenario() -> None:
        trace = MemoryTraceSink()
        adapter = PiRuntimeAdapter((sys.executable, "-c", program), timeout_seconds=0.5 if code != "timeout" else 0.1)
        with pytest.raises(HarnessTurnError, match=code):
            await adapter.run_turn(_context(), _tools(), trace)
        assert trace.events[-1]["eventType"] == "pi.turn_failed"
        assert trace.events[-1]["metadata"] == {"code": code}

    asyncio.run(scenario())


def test_cancelled_real_pi_turn_stops_without_returning_model_text() -> None:
    async def scenario() -> None:
        cancel, trace = asyncio.Event(), MemoryTraceSink()

        async def wait_for_cancel(_arguments: dict[str, object]) -> object:
            await cancel.wait()
            return {"unexpected": True}

        harness = Harness(
            PiRuntimeAdapter(("node", str(SIDECAR))),
            ToolRegistry((ToolDefinition("supply.impact", wait_for_cancel),)),
        )
        task = asyncio.create_task(harness.execute(_context(), trace, cancel))
        while len(trace.events) < 2:
            await asyncio.sleep(0)
        cancel.set()
        result = await task
        assert result.status == "cancelled"
        assert [event["eventType"] for event in trace.events] == ["pi.turn_started", "pi.tool_call", "pi.turn_cancelled"]

    asyncio.run(scenario())


def test_python_tool_failure_is_stable_and_traced() -> None:
    async def scenario() -> None:
        trace = MemoryTraceSink()
        broken = ToolRegistry((ToolDefinition("supply.impact", lambda _arguments: (_ for _ in ()).throw(HarnessToolError("broken"))),))
        with pytest.raises(HarnessTurnError, match="tool_failed"):
            await PiRuntimeAdapter(("node", str(SIDECAR))).run_turn(_context(), broken, trace)
        assert trace.events[-1]["metadata"] == {"code": "binding_invalid"}

    asyncio.run(scenario())


def test_harness_runs_the_same_contract_with_a_non_pi_engine() -> None:
    async def scenario() -> None:
        trace = MemoryTraceSink()
        result = await Harness(PiRuntimeAdapter((sys.executable, str(MINIMAL_ENGINE))), _tools()).execute(_context(), trace)
        assert result.status == "completed"
        assert result.tool_calls == ("supply.impact",)
        assert [event["eventType"] for event in trace.events] == [
            "pi.turn_started",
            "pi.tool_call",
            "pi.model_delta",
            "pi.turn_completed",
        ]

    asyncio.run(scenario())


def test_pinned_pi_version_matches_the_sidecar_lockfile() -> None:
    package = json.loads((ROOT / "pi_sidecar" / "package.json").read_text())
    lockfile = json.loads((ROOT / "pi_sidecar" / "package-lock.json").read_text())
    assert package["dependencies"]["@mariozechner/pi-agent-core"] == PI_CORE_ENGINE.removeprefix("pi-agent-core@")
    assert lockfile["packages"]["node_modules/@mariozechner/pi-agent-core"]["version"] == package["dependencies"]["@mariozechner/pi-agent-core"]
