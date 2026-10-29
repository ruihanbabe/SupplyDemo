"""F51 v2 JSONL host: one sealed model message, one Python scheduler."""

from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from supplyagent.sdk.context.protocol import FrameReceiver, frame
from supplyagent.sdk.engine.harness import (
    MAX_JSONL_BYTES,
    PI_CORE_ENGINE,
    HarnessProtocolError,
    HarnessTurnError,
    TurnResult,
)
from supplyagent.sdk.tools.runtime import CallIdentity, ToolAbort
from supplyagent.sdk.tools.scheduler import ToolScheduler
from supplyagent.service.persistence.context_store import ContextError

TOOL_PROTOCOL = "python-batch-v2"
CALL_ID = re.compile(r"^[a-zA-Z0-9_|-]{1,200}$")


def validate_batch(event, invocation, expected_batch, seen, gateway):
    if set(event) != {
        "protocolVersion",
        "seq",
        "type",
        "turnId",
        "batchId",
        "modelCallKey",
        "calls",
    }:
        raise HarnessProtocolError("batch envelope fields")
    if type(event["batchId"]) is not int or event["batchId"] != expected_batch:
        raise HarnessProtocolError("batch sequence")
    key = event["modelCallKey"]
    if (
        gateway
        and (
            not isinstance(key, str)
            or not re.fullmatch(re.escape(str(invocation)) + r"-(?:[1-9][0-9]{0,2}|1000)", key)
        )
    ) or (not gateway and key is not None):
        raise HarnessProtocolError("model call identity")
    calls = event["calls"]
    if not isinstance(calls, list) or not 1 <= len(calls) <= 16 or len(seen) + len(calls) > 16:
        raise HarnessProtocolError("tool budget")
    batch = []
    local = set()
    for index, call in enumerate(calls):
        if (
            not isinstance(call, dict)
            or set(call) != {"index", "callId", "name", "arguments"}
            or type(call["index"]) is not int
            or call["index"] != index
        ):
            raise HarnessProtocolError("call position")
        call_id = call["callId"]
        name = call["name"]
        if (
            not isinstance(call_id, str)
            or not CALL_ID.fullmatch(call_id)
            or call_id in seen
            or call_id in local
        ):
            raise HarnessProtocolError("duplicate or invalid call ID")
        if not isinstance(name, str) or not 1 <= len(name) <= 96:
            raise HarnessProtocolError("invalid tool name")
        local.add(call_id)
        batch.append(
            (CallIdentity(invocation, call_id, expected_batch, index, key), name, call["arguments"])
        )
    seen.update(local)
    return batch


async def run_batch_turn(adapter, context, tools, trace, cancel):
    process = await adapter._spawn()
    invocation = context.invocation_id or uuid4()
    incoming = outgoing = batch_id = 0
    reported_failure = False
    seen = set()
    calls = []
    started = False
    session = adapter._context_session
    version = adapter._protocol_version
    receiver = FrameReceiver(session.policy.settings) if session else None
    final_receiver = FrameReceiver(session.policy.settings, "final") if session else None
    committed = False
    deadline = asyncio.get_running_loop().time() + adapter._timeout_seconds
    scheduler = ToolScheduler(adapter._scheduler_policy)

    async def send(kind, **values):
        nonlocal outgoing
        outgoing += 1
        await adapter._write(
            process,
            {
                "protocolVersion": version,
                "seq": outgoing,
                "type": kind,
                "turnId": str(invocation),
                **values,
            },
        )

    async def context_operation(awaitable):
        if cancel is None:
            return await awaitable
        operation = asyncio.create_task(awaitable)
        cancellation = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, cancellation}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancellation in done:
                raise ToolAbort("cancelled")
            return await operation
        finally:
            for task in (operation, cancellation):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, cancellation, return_exceptions=True)

    async def cancelled():
        await send("turn.cancel")
        await trace.record("pi.turn_cancelled", {"code": "cancelled"}, incoming + 1)
        return TurnResult(invocation, "cancelled", tuple(calls), incoming + 1)

    try:
        async with asyncio.timeout_at(deadline):
            if session:
                await context_operation(session.start(context, invocation, tools))
            await send(
                "turn.start",
                task=context.task,
                **({"instructions": context.instructions} if context.instructions else {}),
                context={"factReferences": list(context.fact_references)},
                tools=tools.binding.project(wire_names=adapter._gateway_url is not None),
                toolProtocol=TOOL_PROTOCOL,
                bindingVersion=tools.binding.snapshot["bindingVersion"],
                bindingHash=tools.binding.snapshot["bindingHash"],
                **(
                    {
                        "contextProtocol": 1,
                        "contextLimits": {
                            k: session.policy.settings[k]
                            for k in ("maxTransferBytes", "chunkBytes", "maxChunks")
                        },
                    }
                    if session
                    else {}
                ),
            )
            while True:
                event = await adapter._read_or_cancel(process, cancel)
                if event is None:
                    return await cancelled()
                incoming += 1
                if (
                    event.get("protocolVersion") != version
                    or event.get("turnId") != str(invocation)
                    or type(event.get("seq")) is not int
                    or event["seq"] != incoming
                ):
                    raise HarnessProtocolError("v2 envelope/sequence mismatch")
                kind = event.get("type")
                if not started:
                    adapter._exact(
                        event,
                        {"protocolVersion", "seq", "type", "turnId", "engine", "toolProtocol"},
                    )
                    if (
                        kind != "turn.started"
                        or event["engine"] != PI_CORE_ENGINE
                        or event["toolProtocol"] != TOOL_PROTOCOL
                    ):
                        raise HarnessProtocolError("v2 handshake mismatch")
                    started = True
                    await trace.record(
                        "pi.turn_started",
                        {"engine": PI_CORE_ENGINE, "toolProtocol": TOOL_PROTOCOL},
                        incoming,
                    )
                elif session and kind.startswith("context."):
                    if committed:
                        raise HarnessProtocolError("context after final commit")
                    if kind == "context.local.consume":
                        if receiver.active or final_receiver.active:
                            raise HarnessProtocolError("consume during context transfer")
                        await context_operation(session.consume_local(event))
                        await send("context.local.accepted", requestId=event["requestId"])
                    elif kind.startswith("context.input."):
                        if final_receiver.active is not None:
                            raise HarnessProtocolError("overlapping context transfer")
                        result = receiver.accept(event)
                        if result:
                            header, body = result
                            identifier, projection, ready = await context_operation(
                                session.prepare(header, body)
                            )
                            for item in frame(
                                projection,
                                header["requestId"],
                                header["modelCallKey"],
                                "output",
                                snapshot_id=identifier,
                                limits=session.policy.settings,
                            ):
                                await send(item.pop("type"), **item)
                            await trace.record(
                                "pi.context_prepared",
                                {
                                    k: ready[k]
                                    for k in (
                                        "snapshotId",
                                        "modelCallKey",
                                        "projectionHash",
                                        "policyHash",
                                        "epoch",
                                        "budget",
                                    )
                                },
                                incoming,
                            )
                            await send("context.ready", **ready)
                    elif kind.startswith("context.final."):
                        if receiver.active is not None:
                            raise HarnessProtocolError("overlapping context transfer")
                        result = final_receiver.accept(event)
                        if result:
                            header, body = result
                            if header["modelCallKey"] != f"{invocation}-{session.ordinal}":
                                raise ContextError("context_call_key_invalid")
                            await context_operation(session.finish(body))
                            committed = True
                            await send("context.committed", requestId=header["requestId"])
                    else:
                        raise HarnessProtocolError("unexpected context event")
                elif kind == "tool.batch":
                    if session and (receiver.active or final_receiver.active or committed):
                        raise HarnessProtocolError("tool during context transfer")
                    batch_id += 1
                    batch = validate_batch(
                        event, invocation, batch_id, seen, adapter._gateway_url is not None
                    )
                    await trace.record(
                        "pi.tool_batch",
                        {
                            "batchId": batch_id,
                            "callCount": len(batch),
                            "modelCallKey": event["modelCallKey"],
                        },
                        incoming,
                    )
                    outcomes = await scheduler.run_batch(batch, tools.runtime, cancel, deadline)
                    # Recheck the whole batch immediately before delivery, including refusals.
                    if tools.runtime.store.durable:
                        await tools.runtime.store.check_delivery(outcomes)
                    if session:
                        session.remember_batch(batch, outcomes)
                    calls.extend(name for _, name, _ in batch)
                    results = [
                        {"index": i, "callId": batch[i][0].call_id, **r.wire()}
                        for i, r in enumerate(outcomes)
                    ]
                    wire = {
                        "protocolVersion": version,
                        "seq": outgoing + 1,
                        "type": "tool.batch.result",
                        "turnId": str(invocation),
                        "batchId": batch_id,
                        "results": results,
                    }
                    if (
                        session
                        and len((json.dumps(wire, separators=(",", ":")) + "\n").encode())
                        > MAX_JSONL_BYTES
                    ):
                        for item in results:
                            if item["status"] == "ok" and item["resultRef"]:
                                item["modelPayload"] = {
                                    "citation": item["resultRef"],
                                    "omitted": True,
                                    "readRequired": True,
                                }
                    await send("tool.batch.result", batchId=batch_id, results=results)
                else:
                    legacy = {k: v for k, v in event.items() if k != "seq"}
                    legacy["protocolVersion"] = 1
                    adapter._validate_event(legacy, invocation, "tool.call_or_complete")
                    if kind == "tool.call" or kind == "turn.started":
                        raise HarnessProtocolError("v1 event in v2")
                    await trace.record(
                        f"pi.{kind.replace('.', '_')}", adapter._event_metadata(event), incoming
                    )
                    if kind == "model.delta":
                        continue
                    if kind == "turn.failed":
                        reported_failure = True
                        raise HarnessTurnError(str(event["code"]))
                    if kind == "turn.completed" and session and not committed:
                        raise HarnessProtocolError("final context commit required")
                    if kind in {"turn.completed", "turn.cancelled"}:
                        await adapter._close_stdin(process)
                        await adapter._wait(process)
                        if (await process.stdout.read(MAX_JSONL_BYTES + 1)).strip():
                            raise HarnessProtocolError("events after terminal")
                        return TurnResult(
                            invocation,
                            "completed" if kind == "turn.completed" else "cancelled",
                            tuple(calls),
                            incoming,
                            event.get("output"),
                        )
    except ContextError as error:
        await trace.record("pi.turn_failed", {"code": error.code}, incoming + 1)
        raise HarnessTurnError("model_failed", reason=error.code) from None
    except ToolAbort as error:
        if error.code == "cancelled":
            return await cancelled()
        code = "timeout" if error.code == "deadline_exceeded" else "tool_failed"
        await trace.record("pi.turn_failed", {"code": error.code}, incoming + 1)
        raise HarnessTurnError(code, reason=error.code) from None
    except HarnessTurnError as error:
        if not reported_failure:
            await trace.record("pi.turn_failed", {"code": error.code}, incoming + 1)
        raise
    except HarnessProtocolError:
        await trace.record("pi.turn_failed", {"code": "protocol_failed"}, incoming + 1)
        raise
    except TimeoutError:
        await trace.record("pi.turn_failed", {"code": "timeout"}, incoming + 1)
        raise HarnessTurnError("timeout") from None
    finally:
        if process.returncode is None:
            await adapter._terminate(process)
        if session and hasattr(session, "invocation"):
            try:
                async with asyncio.timeout(2):
                    await session.abandon()
            except (ContextError, TimeoutError):
                await trace.record(
                    "pi.context_cleanup_failed", {"code": "context_cleanup_failed"}, incoming + 1
                )
