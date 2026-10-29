"""Real pinned Pi subprocesses using complete multi-call Responses/GLM streams."""

import asyncio
import json
from uuid import uuid4

import pytest
from test_product_api_gateway import run_pi_fixture

from product_api.harness import HarnessProtocolError, HarnessTurnError, ToolDefinition
from product_api.tool_bridge import validate_batch
from product_api.tool_scheduler import SchedulerPolicy


class BatchProvider:
    def __init__(self, glm=False, invalid=False, count=3):
        self.glm, self.invalid, self.count = glm, invalid, count
        self.requests = []

    async def stream(self, _policy, payload):
        self.requests.append(payload)
        if self.glm:
            results = [m for m in payload["messages"] if m["role"] == "tool"]
            values = [json.loads(m["content"]) for m in results]
        else:
            results = [m for m in payload["input"] if m.get("type") == "function_call_output"]
            values = [json.loads(m["output"]) for m in results]
        call_specs = []
        if not results:
            for i in range(self.count):
                name = "unknown_tool" if self.invalid and i == 0 else "supply_impact"
                args = {"partNumber": 1 if self.invalid and i == 1 else f"PART-{i}"}
                call_specs.append((f"call_{i}", name, args))
        elif self.invalid and len(results) == 3:
            assert values == [
                {"error": "unknown_tool"},
                {"error": "invalid_arguments"},
                {"partNumber": "PART-2"},
            ]
            call_specs = [("corrected", "supply_impact", {"partNumber": "FIXED"})]
        else:
            if not self.invalid:
                assert values == [{"partNumber": f"PART-{i}"} for i in range(self.count)]
            else:
                assert values[-1] == {"partNumber": "FIXED"}
        text = json.dumps({"ok": True})
        if self.glm:
            delta = (
                {
                    "tool_calls": [
                        {
                            "index": i,
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (cid, name, args) in enumerate(call_specs)
                    ]
                }
                if call_specs
                else {"content": text}
            )
            yield {
                "id": f"glm_{len(self.requests)}",
                "model": "glm-4.7-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": "tool_calls" if call_specs else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        else:
            output = []
            if call_specs:
                for i, (cid, name, args) in enumerate(call_specs):
                    item = {
                        "type": "function_call",
                        "id": f"fc_{cid}",
                        "call_id": cid,
                        "name": name,
                        "arguments": json.dumps(args),
                    }
                    yield {
                        "type": "response.output_item.added",
                        "output_index": i,
                        "item": {**item, "arguments": ""},
                    }
                    yield {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item["id"],
                        "output_index": i,
                        "delta": item["arguments"],
                    }
                    yield {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "arguments": item["arguments"],
                    }
                    yield {"type": "response.output_item.done", "output_index": i, "item": item}
                    output.append(item)
            else:
                item = {
                    "type": "message",
                    "id": "msg",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
                yield {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "content": []},
                }
                yield {
                    "type": "response.output_text.delta",
                    "item_id": "msg",
                    "content_index": 0,
                    "delta": text,
                }
                yield {"type": "response.output_item.done", "output_index": 0, "item": item}
                output = [item]
            yield {
                "type": "response.completed",
                "response": {
                    "id": f"resp_{len(self.requests)}",
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            }


@pytest.mark.parametrize("glm", [False, True])
@pytest.mark.parametrize("strategy", ["serial", "contiguous"])
def test_complete_three_call_message_runs_once_with_ordered_results(glm, strategy):
    async def scenario():
        order = []

        async def handler(a):
            part = a["partNumber"]
            order.append(("start", part))
            await asyncio.sleep(0.03 if part == "PART-0" else 0)
            order.append(("end", part))
            return a

        provider = BatchProvider(glm)
        result, trace, _ = await run_pi_fixture(
            provider,
            ToolDefinition("supply.impact", handler),
            "batch",
            glm=glm,
            protocol_version=2,
            scheduler_policy=SchedulerPolicy(strategy),
        )
        assert result.status == "completed" and result.output == {"ok": True}
        assert len(result.tool_calls) == 3 and len(provider.requests) == 2
        batches = [e for e in trace.events if e["eventType"] == "pi.tool_batch"]
        assert len(batches) == 1 and batches[0]["metadata"]["callCount"] == 3
        assert len(order) == 6
        if strategy == "contiguous":
            assert order.index(("end", "PART-1")) < order.index(("end", "PART-0"))
        else:
            assert order.index(("end", "PART-0")) < order.index(("start", "PART-1"))

    asyncio.run(scenario())


@pytest.mark.parametrize("glm", [False, True])
def test_local_pi_rejections_are_replaced_before_model_correction(glm):
    async def scenario():
        executed = []

        async def handler(a):
            executed.append(a)
            return a

        provider = BatchProvider(glm, invalid=True)
        result, trace, _ = await run_pi_fixture(
            provider,
            ToolDefinition("supply.impact", handler),
            "correct",
            glm=glm,
            protocol_version=2,
        )
        assert result.status == "completed" and len(provider.requests) == 3
        assert executed == [{"partNumber": "PART-2"}, {"partNumber": "FIXED"}]
        assert len([e for e in trace.events if e["eventType"] == "pi.tool_batch"]) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("glm", [False, True])
def test_oversized_batch_never_executes(glm):
    async def scenario():
        executed = []
        with pytest.raises((HarnessTurnError, HarnessProtocolError)):
            await run_pi_fixture(
                BatchProvider(glm, count=17),
                ToolDefinition("supply.impact", lambda a: executed.append(a)),
                "too many",
                glm=glm,
                protocol_version=2,
            )
        assert not executed

    asyncio.run(scenario())


def test_batch_identity_positions_and_total_budget_fail_closed():
    inv = uuid4()
    good = {
        "protocolVersion": 2,
        "seq": 2,
        "type": "tool.batch",
        "turnId": str(inv),
        "batchId": 1,
        "modelCallKey": None,
        "calls": [{"index": 0, "callId": "call|fc", "name": "supply.impact", "arguments": {}}],
    }
    assert validate_batch(good, inv, 1, set(), False)[0][0].call_id == "call|fc"
    for change in (
        {"batchId": 2},
        {"modelCallKey": "fake"},
        {"extra": True},
        {"calls": []},
        {"calls": [{**good["calls"][0], "index": 1}]},
        {"calls": good["calls"] * 2},
    ):
        with pytest.raises(HarnessProtocolError):
            validate_batch({**good, **change}, inv, 1, set(), False)
    with pytest.raises(HarnessProtocolError):
        validate_batch(good, inv, 1, {"call|fc"}, False)
    with pytest.raises(HarnessProtocolError):
        validate_batch(good, inv, 1, {str(i) for i in range(16)}, False)


@pytest.mark.parametrize("glm", [False, True])
def test_cancellation_stops_inflight_python_calls_and_sidecar(glm):
    async def scenario():
        cancel = asyncio.Event()
        entered = asyncio.Event()
        stopped = []

        async def handler(a):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(a["partNumber"])

        async def stop():
            await entered.wait()
            cancel.set()

        trigger = asyncio.create_task(stop())
        try:
            result, _, _ = await run_pi_fixture(
                BatchProvider(glm),
                ToolDefinition("supply.impact", handler),
                "cancel",
                glm=glm,
                protocol_version=2,
                cancel_event=cancel,
            )
            assert result.status == "cancelled" and len(stopped) == 3
        finally:
            trigger.cancel()
            await asyncio.gather(trigger, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["seq", "version", "duplicate", "index", "bytes", "budget", "terminal"]
)
def test_untrusted_v2_stream_is_rejected_before_domain_execution(fault):
    import sys

    from product_api.harness import HarnessContext, MemoryTraceSink, PiRuntimeAdapter, ToolRegistry

    program = r"""
import json,sys
start=json.loads(sys.stdin.readline());fault=sys.argv[1]
def send(type,seq,**kw):print(json.dumps({'protocolVersion':2,'seq':seq,'turnId':start['turnId'],'type':type,**kw}),flush=True)
if fault=='seq':send('turn.started',2,engine='pi-agent-core@0.73.1',toolProtocol='python-batch-v2')
elif fault=='version':print(json.dumps({'protocolVersion':1,'seq':1,'turnId':start['turnId'],'type':'turn.started'}),flush=True)
else:
 send('turn.started',1,engine='pi-agent-core@0.73.1',toolProtocol='python-batch-v2')
 if fault=='terminal':
  send('turn.completed',2,status='completed');send('turn.completed',3,status='completed');sys.stdin.read();sys.exit(0)
 calls=[{'index':0,'callId':'a','name':'supply.impact','arguments':{'partNumber':'A'}}]
 if fault=='duplicate':calls.append({**calls[0],'index':1})
 if fault=='index':calls[0]['index']=1
 if fault=='bytes':calls[0]['arguments']['partNumber']='x'*17000
 if fault=='budget':calls=[{**calls[0],'index':i,'callId':str(i)} for i in range(17)]
 send('tool.batch',2,batchId=1,modelCallKey=None,calls=calls)
sys.stdin.read()
"""

    async def scenario():
        executed = []
        adapter = PiRuntimeAdapter(
            (sys.executable, "-c", program, fault), protocol_version=2, timeout_seconds=2
        )
        with pytest.raises(HarnessProtocolError):
            await adapter.run_turn(
                HarnessContext(uuid4(), uuid4(), None, "bad"),
                ToolRegistry((ToolDefinition("supply.impact", lambda a: executed.append(a)),)),
                MemoryTraceSink(),
            )
        assert not executed

    asyncio.run(scenario())


def test_oversized_results_terminate_without_another_model_request():
    async def scenario():
        provider = BatchProvider()
        executed = []

        def handler(a):
            executed.append(a)
            return {"body": "x" * 6000}

        with pytest.raises(HarnessProtocolError, match="超限"):
            await run_pi_fixture(
                provider,
                ToolDefinition("supply.impact", handler),
                "result limit",
                protocol_version=2,
            )
        assert len(executed) == 3 and len(provider.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing", "duplicate", "stale", "position", "call_id", "seq"])
def test_real_sidecar_rejects_mismatched_python_results(fault):
    from pathlib import Path

    from product_api.harness import PiRuntimeAdapter
    from product_api.tool_registry import TOOL_REGISTRY

    async def scenario():
        adapter = PiRuntimeAdapter(
            (
                "node",
                str(Path(__file__).resolve().parents[1] / "pi_sidecar/f42_protocol_sidecar.mjs"),
            ),
            protocol_version=2,
        )
        process = await adapter._spawn()
        turn = str(uuid4())
        binding = TOOL_REGISTRY.bind(("supply.impact",))
        start = {
            "protocolVersion": 2,
            "seq": 1,
            "type": "turn.start",
            "turnId": turn,
            "task": "fixture",
            "context": {"factReferences": []},
            "tools": binding.project(),
            "toolProtocol": "python-batch-v2",
            "bindingVersion": 1,
            "bindingHash": binding.snapshot["bindingHash"],
        }
        try:
            await adapter._write(process, start)
            assert (await adapter._read(process))["type"] == "turn.started"
            batch = await adapter._read(process)
            assert batch["type"] == "tool.batch"
            result = {
                "index": 0,
                "callId": batch["calls"][0]["callId"],
                "status": "ok",
                "code": None,
                "modelPayload": {},
                "resultRef": None,
            }
            message = {
                "protocolVersion": 2,
                "seq": 2,
                "type": "tool.batch.result",
                "turnId": turn,
                "batchId": 1,
                "results": [result],
            }
            if fault == "missing":
                message["results"] = []
            elif fault == "duplicate":
                message["results"] = [result, result]
            elif fault == "stale":
                message["batchId"] = 0
            elif fault == "position":
                result["index"] = 1
            elif fault == "call_id":
                result["callId"] = "other"
            else:
                message["seq"] = 3
            await adapter._write(process, message)
            assert await asyncio.wait_for(process.wait(), 3) != 0
            assert not (await process.stdout.read()).strip()
        finally:
            if process.returncode is None:
                await adapter._terminate(process)

    asyncio.run(scenario())
