"""C2 public Compiler/framing contracts; no model or external memory calls."""

import asyncio
import copy
import json
from uuid import uuid4

import pytest

from product_api.context_protocol import FrameReceiver, frame
from product_api.context_runtime import ContextCompiler, ContextPolicy, canonical, complete_units
from product_api.context_store import ContextError


def tool_history(size=100):
    return [
        {"role": "user", "content": "调查未确认的到货时间，禁止承诺交期"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "id": "raw|item",
                    "name": "supply_impact",
                    "arguments": {"partNumber": "A"},
                },
            ],
        },
        {
            "role": "toolResult",
            "toolCallId": "raw|item",
            "content": [
                {
                    "type": "text",
                    "text": canonical(
                        {"body": "中" * size, "citation": {"sourceId": str(uuid4())}}
                    ),
                },
            ],
            "isError": False,
        },
        {"role": "assistant", "content": [{"type": "text", "text": "交期仍未知。"}]},
    ]


def test_compiler_preserves_sources_pairs_and_unknowns_while_shrinking_old_results():
    messages = tool_history(3000)
    original = copy.deepcopy(messages)
    settings = ContextPolicy.load().settings
    settings["inputCap"] = 2000
    result = asyncio.run(
        ContextCompiler(ContextPolicy.from_dict(settings)).prepare(
            messages,
            "仅使用证据",
            [],
            {"contextWindow": 32768, "maxTokens": 128},
        )
    )
    assert messages == original
    assert result["memoryStatus"] == "disabled" and result["memoryItems"] == []
    assert result["messages"][0] == original[0] and result["messages"][-1] == original[-1]
    assert len(complete_units(result["messages"])[1]) == 2
    omitted = json.loads(result["messages"][2]["content"][0]["text"])
    assert omitted["readRequired"] and omitted["omitted"]
    assert omitted["resultRef"] == json.loads(original[2]["content"][0]["text"])["citation"]
    assert result["manifest"][1]["action"] == "result_reference"
    assert result["budget"]["inputEstimate"] < result["budget"]["inputLimit"]


def test_compiler_recomputes_actual_window_schema_and_working_reserves():
    compiler = ContextCompiler()
    messages = tool_history()

    async def prepare(profile, tools=(), working=None):
        return await compiler.prepare(messages, "rules", list(tools), profile, working=working)

    large = asyncio.run(prepare({"contextWindow": 8000, "maxTokens": 1000}))
    small = asyncio.run(prepare({"contextWindow": 4000, "maxTokens": 1000}))
    assert large["budget"]["inputLimit"] - small["budget"]["inputLimit"] == 3600
    schema = [{"name": "tool", "schema": "x" * 500}]
    with_schema = asyncio.run(prepare({"contextWindow": 8000, "maxTokens": 1000}, schema))
    assert with_schema["budget"]["inputLimit"] < large["budget"]["inputLimit"]
    state = asyncio.run(
        prepare({"contextWindow": 8000, "maxTokens": 1000}, working={"unknowns": ["unconfirmed"]})
    )
    assert state["messages"][0]["role"] == "user"
    assert state["systemPrompt"] == "rules"
    with pytest.raises(ContextError, match="context_overflow"):
        asyncio.run(prepare({"contextWindow": 1024, "maxTokens": 1000}))
    with pytest.raises(ContextError, match="context_overflow"):
        asyncio.run(
            prepare({"contextWindow": 4000, "maxTokens": 1000}, working={"goal": "大" * 2000})
        )
    # The newest evidence cannot be silently removed to make a request fit.
    with pytest.raises(ContextError, match="context_overflow"):
        asyncio.run(
            compiler.prepare(
                tool_history(4000)[:-1], "rules", [], {"contextWindow": 4000, "maxTokens": 1000}
            )
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop(2),
        lambda m: m[2].update(toolCallId="other"),
        lambda m: m[1]["content"].append(copy.deepcopy(m[1]["content"][0])),
        lambda m: m[1]["content"].append("not a block"),
        lambda m: m.append({"role": "toolResult", "toolCallId": "dangling"}),
    ],
)
def test_complete_units_rejects_partial_or_malformed_pairs(mutate):
    messages = tool_history()
    mutate(messages)
    with pytest.raises(ContextError):
        complete_units(messages)


def test_framing_multibyte_large_transfer_roundtrip_and_line_bound():
    body = {"messages": [{"role": "user", "content": "中🙂" * 20000}]}
    events = list(frame(body, "request_1", f"{uuid4()}-1", "input"))
    assert len(events) > 3
    receiver = FrameReceiver()
    for i, event in enumerate(events):
        envelope = {"protocolVersion": 3, "seq": i + 1, "turnId": str(uuid4()), **event}
        assert len(json.dumps(envelope).encode()) < 16384
        result = receiver.accept(envelope)
        if i < len(events) - 1:
            assert result is None
    assert result[1] == body and receiver.active is None


@pytest.mark.parametrize(
    "damage", ["order", "hash", "missing", "identity", "extra", "count", "oversized"]
)
def test_framing_fails_closed_without_returning_partial_body(damage):
    events = list(frame({"messages": ["中" * 9000]}, "r", f"{uuid4()}-1", "input"))
    if damage == "order":
        events[1], events[2] = events[2], events[1]
    elif damage == "hash":
        events[0]["sha256"] = events[-1]["sha256"] = "0" * 64
    elif damage == "missing":
        events.pop(1)
    elif damage == "identity":
        events[1]["requestId"] = "other"
    elif damage == "extra":
        events[1]["file"] = "/unauthorized"
    elif damage == "count":
        events[0]["chunkCount"] = 129
    elif damage == "oversized":
        events[1]["data"] = "A" * 20000
    receiver = FrameReceiver()
    with pytest.raises(ContextError):
        for event in events:
            assert receiver.accept(event) is None


def test_policy_is_immutable_and_rejects_invalid_transfer_and_partition_config():
    policy = ContextPolicy.load()
    changed = policy.settings
    changed["partitions"]["memory"] = 99
    assert policy.settings["partitions"]["memory"] == 10
    with pytest.raises(ValueError):
        ContextPolicy.from_dict(changed)
    changed = policy.settings
    changed["chunkBytes"] = True
    with pytest.raises(ValueError):
        ContextPolicy.from_dict(changed)
    with pytest.raises(ContextError, match="context_transfer_limit"):
        list(frame({"text": "x" * 1048576}, "r", f"{uuid4()}-1", "input"))


@pytest.mark.parametrize("damage", [None, "hash", "order", "early_ready"])
def test_node_receiver_validates_the_same_framing_before_ready(damage):
    import subprocess
    from pathlib import Path

    key, snapshot = f"{uuid4()}-1", uuid4()
    projection = {"messages": [{"role": "user", "content": "中🙂" * 5000}]}
    events = list(frame(projection, "r", key, "output", snapshot_id=snapshot))
    ready = {
        "type": "context.ready",
        "requestId": "r",
        "snapshotId": str(snapshot),
        "modelCallKey": key,
        "projectionHash": "a" * 64,
        "policyHash": "b" * 64,
        "epoch": 0,
        "budget": {},
    }
    if damage == "hash":
        events[0]["sha256"] = events[-1]["sha256"] = "0" * 64
    elif damage == "order":
        events[1], events[2] = events[2], events[1]
    if damage == "early_ready":
        events.insert(1, ready)
    else:
        events.append(ready)
    script = """
        import { ContextBridge } from "./pi_sidecar/context_protocol.mjs";
        let raw = "";
        for await (const chunk of process.stdin) raw += chunk;
        const fixture = JSON.parse(raw);
        const bridge = new ContextBridge({turnId: "fixture", contextProtocol: 1,
          contextLimits: {maxTransferBytes: 1048576, chunkBytes: 8192, maxChunks: 128}}, () => {});
        const result = bridge.transfer("input", [], fixture.key);
        const requestId = bridge.pending.requestId;
        try {
          for (const event of fixture.events) bridge.receive({...event, requestId});
          const value = await result;
          process.stdout.write(JSON.stringify(value.projection));
        } catch {
          process.stdout.write("rejected");
        }
    """
    outcome = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps({"key": key, "events": events}),
        text=True,
        check=False,
        capture_output=True,
        timeout=5,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stdout == "rejected" if damage else json.loads(outcome.stdout) == projection
