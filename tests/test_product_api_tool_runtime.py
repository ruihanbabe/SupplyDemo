"""Runtime identity, immutable preparation and failure boundaries."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from product_api.harness import ToolDefinition
from product_api.tool_registry import TOOL_REGISTRY
from product_api.tool_runtime import (
    CURRENT_TOOL_CALL,
    CallIdentity,
    MemoryExecutionStore,
    ToolAbort,
    ToolOutcome,
    ToolRuntime,
)


def test_preparation_is_immutable_and_identity_is_task_local():
    async def scenario():
        seen = []

        async def handler(args):
            await asyncio.sleep(0)
            seen.append((CURRENT_TOOL_CALL.get()["toolCallId"], args["partNumber"]))
            return args

        runtime = ToolRuntime(
            (ToolDefinition("supply.impact", handler),), TOOL_REGISTRY.bind(("supply.impact",))
        )
        args = {"partNumber": "A"}
        a = await runtime.prepare(CallIdentity(uuid4(), "a"), "supply_impact", args)
        args["partNumber"] = "changed"
        b = await runtime.prepare(CallIdentity(uuid4(), "b"), "supply.impact", {"partNumber": "B"})
        await asyncio.gather(runtime.execute(a), runtime.execute(b))
        assert seen == [("a", "A"), ("b", "B")]
        assert CURRENT_TOOL_CALL.get() is None
        with pytest.raises(ToolAbort, match="duplicate_call"):
            await runtime.execute(a)
        with pytest.raises(ToolAbort, match="duplicate_call"):
            await runtime.execute(replace(b, owner=object()))

    asyncio.run(scenario())


def test_limited_feedback_new_call_identity_and_cancellation():
    async def scenario():
        executed = []
        runtime = ToolRuntime(
            (ToolDefinition("supply.impact", lambda a: executed.append(a)),),
            TOOL_REGISTRY.bind(("supply.impact",)),
        )
        identity = CallIdentity(uuid4(), "bad")
        rejected = await runtime.prepare(identity, "other.tool", {})
        assert rejected == ToolOutcome("error", "unknown_tool", {"error": "unknown_tool"})
        with pytest.raises(ToolAbort, match="duplicate_call"):
            await runtime.prepare(identity, "supply.impact", {"partNumber": "A"})
        invalid = await runtime.prepare(
            replace(identity, call_id="invalid"), "supply.impact", {"partNumber": 1}
        )
        assert invalid.code == "invalid_arguments"
        prepared = await runtime.prepare(
            replace(identity, call_id="new"), "supply.impact", {"partNumber": "A"}
        )
        cancel = asyncio.Event()
        cancel.set()
        with pytest.raises(ToolAbort, match="cancelled"):
            await runtime.execute(prepared, cancel)
        assert not executed

    asyncio.run(scenario())


def test_service_failure_is_recoverable_but_unpersisted_success_is_fatal():
    class Durable(MemoryExecutionStore):
        durable = True

    async def scenario():
        async def unavailable(_):
            raise ConnectionError("must not leak")

        binding = TOOL_REGISTRY.bind(("supply.impact",))
        r = ToolRuntime((ToolDefinition("supply.impact", unavailable),), binding)
        p = await r.prepare(CallIdentity(uuid4(), "network"), "supply.impact", {"partNumber": "A"})
        assert (await r.execute(p)).payload == {"error": "service_unavailable"}
        r = ToolRuntime(
            (ToolDefinition("supply.impact", lambda _: {}, execution_store=Durable()),), binding
        )
        p = await r.prepare(CallIdentity(uuid4(), "missing"), "supply.impact", {"partNumber": "A"})
        with pytest.raises(ToolAbort, match="persistence_failed"):
            await r.execute(p)

    asyncio.run(scenario())


def test_returned_payload_must_match_the_atomically_accepted_payload():
    from product_api.tool_runtime import accept_tool_result

    class Store(MemoryExecutionStore):
        durable = True

        async def accept(self, *_):
            pass

    async def scenario():
        async def changed(_):
            await accept_tool_result(None, {"value": "saved"})
            return {"value": "changed"}

        store = Store()
        r = ToolRuntime(
            (ToolDefinition("supply.impact", changed, execution_store=store),),
            TOOL_REGISTRY.bind(("supply.impact",)),
        )
        p = await r.prepare(CallIdentity(uuid4(), "a"), "supply.impact", {"partNumber": "A"})
        with pytest.raises(ToolAbort, match="persistence_failed"):
            await r.execute(p)

    asyncio.run(scenario())
