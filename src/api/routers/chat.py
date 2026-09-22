"""Streaming chat with server-controlled tool use.

The loop here is bounded and owned by the server. The model chooses *which* injected
tool to call; it never chooses how many rounds to run, never reaches a tool directly and
never sees one it was not given. That is the deliberate difference from a free-running
agent loop: DECISIONS.md D02 keeps step selection with explicit rules, not with the
model, so a model cannot talk its way into another round or another capability.

Scope: this is the conversational entry point, not the full runtime. It does not yet
compile a ContextBundle (F14), write Evidence (F12) or resume after a restart (F15).
Each of those replaces a piece of what is here, and the seams are marked below.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from api.dependencies import get_engine
from contracts.llm import ChatMessage, ModelError, ModelRequest
from infrastructure.agent_config import load_agent_config
from infrastructure.llm import backend_for, load_settings
from infrastructure.replay import RecordingBackend, ReplayBackend
from persistence.procurement import ProcurementRepository
from persistence.tool_audit import PostgresAuditSink
from tools.catalog_tools import ToolContext, build_registry
from tools.registry import Invocation

logger = logging.getLogger("supplyagent.chat")
router = APIRouter(prefix="/api/chat", tags=["chat"])

#: The model gets at most this many tool rounds before it must answer with what it has.
#: A cap is not a performance tweak: without one, a model that keeps asking for the same
#: tool spends the user's budget indefinitely.
MAX_TOOL_ROUNDS = 3

#: The worker whose config/agents/*.yaml supplies model and sampling for this entry.
WORKER = "supervisor"

#: Which tool set this entry may see. Scenario and worker together decide what gets
#: injected, so a tool outside them is not merely discouraged — it is never offered and
#: is refused again at dispatch if the model names it anyway.
SCENARIO = "procurement"

SYSTEM_PROMPT = """你是元件供应风险预警系统的对话入口。

硬约束，任何情况下不得违反：
1. 数量、金额、库存、交期、行数这类事实值，只能来自工具返回的内容。你不得自行计算、
   推断或补齐任何数字；工具没给的就说没有。
2. 工具返回带 status：ok 表示完整，partial 表示只看到样例，not_found 表示确实没有，
   error 表示查询失败。这四种必须如实转述，不得把 error 说成"没有"。
3. 你没有写入权限。涉及下单、创建草稿的请求，说明需要人工批准且当前未接入。
4. 不确定就说不确定，并说明需要补充什么。
"""


class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    #: Prior turns travel from the client for now. This is exactly what F14 replaces:
    #: a compiled ContextBundle built from authoritative state, not from the browser.
    #: Until then the browser is trusted for conversation text only — never for a
    #: business fact, which is why every number on screen comes back through a tool.
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _select_backend():
    """Real backend when explicitly enabled, replay otherwise.

    Replay is not a fallback for a failing backend — a failure must surface. It is the
    choice made when nobody has authorised spending money (SUPPLYAGENT_LLM_ENABLED), and
    it answers only from recordings: an unrecorded question fails loudly rather than
    being improvised.

    The model name comes from the worker config either way, because it is part of the
    replay key: a recording made against one model must not answer for another.
    """
    config = load_agent_config(WORKER)
    settings = load_settings()
    if not settings.enabled:
        return ReplayBackend(), "replay", config
    backend = backend_for(WORKER)
    if os.getenv("SUPPLYAGENT_LLM_RECORD", "").strip().lower() == "true":
        # Recording reuses this exact loop rather than a parallel script, so a recording
        # can never be made from a request shape the real path does not produce. It is
        # off by default and never wraps replay: replay has nothing to record.
        return RecordingBackend(backend), backend.name, config
    return backend, backend.name, config


def _open_run(trace_id: str) -> UUID:
    """One chat turn is one Run. Auditing it is not optional even in a spike.

    The id stays a UUID all the way through. Carrying it as a string works until it is
    compared with one read back from the database, which comes back as a UUID — and then
    two values that denote the same run stop being equal.
    """
    run_id = uuid4()
    with get_engine().begin() as connection:
        connection.execute(
            text("""INSERT INTO run(run_id, trigger_kind, state)
                    VALUES (:run_id, 'user', 'analyzing')"""), {"run_id": run_id})
        connection.execute(
            text("""INSERT INTO run_state_event(run_id, to_state, reason, evidence_ref)
                    VALUES (:run_id, 'analyzing', 'chat turn started', :ref)"""),
            {"run_id": run_id, "ref": json.dumps({"kind": "trace", "id": trace_id})})
    return run_id


def _record_call(run_id: UUID, trace_id: str, model: str, completion) -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text("""INSERT INTO llm_call(llm_call_id, run_id, trace_id, worker, model,
                                         prompt_tokens, output_tokens, duration_ms)
                    VALUES (:id, :run_id, :trace_id, :worker, :model, :prompt, :output, :ms)"""),
            {"id": str(uuid4()), "run_id": run_id, "trace_id": trace_id, "worker": WORKER,
             "model": model, "prompt": completion.usage.prompt_tokens,
             "output": completion.usage.output_tokens, "ms": completion.latency_ms})


def _dispatch(registry, call, run_id: UUID, trace_id: str):
    """One tool call, one transaction.

    Short and self-contained rather than one transaction for the whole turn: a tool that
    writes (a demand, a run, a shortage snapshot) should be durable the moment it
    succeeds, and a model failure three rounds later must not roll it back. The audit row
    is written in the same transaction as the work it describes, so the two cannot
    disagree about whether the call happened.
    """
    with get_engine().begin() as connection:
        return registry.dispatch(call, Invocation(
            data=ToolContext(repository=ProcurementRepository(connection), run_id=run_id),
            run_id=run_id, trace_id=trace_id, worker=WORKER, scenario=SCENARIO,
            audit=PostgresAuditSink(connection)))


def _conversation(request: ChatRequest) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=SYSTEM_PROMPT)]
    messages += [ChatMessage(role=turn.role, content=turn.content) for turn in request.history]
    messages.append(ChatMessage(role="user", content=request.message))
    return messages


def _events(payload: ChatRequest, trace_id: str) -> Iterator[str]:
    try:
        backend, backend_name, config = _select_backend()
    except ModelError as exc:
        yield _sse("error", {"code": exc.code, "message": str(exc)})
        return
    model = config.model

    registry = build_registry()
    tools = registry.specs(effects=("read",), scenario=SCENARIO, worker=WORKER)
    messages = _conversation(payload)

    try:
        run_id = _open_run(trace_id)
    except Exception as exc:
        logger.exception("Could not open a run for this chat turn")
        yield _sse("error", {"code": "audit_unavailable", "message": str(exc)[:200]})
        return

    yield _sse("meta", {"run_id": str(run_id), "trace_id": trace_id, "provider": backend_name,
                        "model": model,
                        "provenance": "replay" if backend_name == "replay" else "real",
                        "tools": [tool.name for tool in tools]})

    for round_index in range(MAX_TOOL_ROUNDS + 1):
        last_round = round_index == MAX_TOOL_ROUNDS
        # The last round is offered no tools at all: the cap has to be enforced by
        # what the model can reach, not by asking it politely to stop.
        request = ModelRequest(
            call_id=str(uuid4()),
            model=config.model,
            messages=tuple(messages),
            tools=() if last_round else tuple(tools),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            stream=True,
            timeout_seconds=config.timeout_seconds,
            trace_id=trace_id,
        )
        completion = None
        try:
            for event in backend.stream(request):
                if event.kind == "text" and event.text:
                    yield _sse("text", {"text": event.text})
                elif event.kind == "done":
                    completion = event.result
        except ModelError as exc:
            yield _sse("error", {"code": exc.code, "message": str(exc),
                                 "retryable": exc.retryable})
            return

        if completion is None:
            yield _sse("error", {"code": "malformed_response",
                                 "message": "Stream ended without a completion"})
            return
        _record_call(run_id, trace_id, model, completion)

        if not completion.wants_tools:
            yield _sse("done", {"run_id": str(run_id), "rounds": round_index + 1,
                                "finish_reason": completion.finish_reason,
                                "output_tokens": completion.usage.output_tokens})
            return

        messages.append(ChatMessage(role="assistant", content=completion.content,
                                    tool_calls=completion.tool_calls))
        for call in completion.tool_calls:
            outcome = _dispatch(registry, call, run_id, trace_id)
            yield _sse("tool", {"name": call.name, "arguments": call.arguments,
                                "status": outcome.status, "error_code": outcome.error_code,
                                "message": outcome.message, "content": outcome.content,
                                "render": outcome.render, "provenance": outcome.provenance})
            messages.append(ChatMessage(role="tool", content=outcome.for_model(),
                                        tool_call_id=call.id))

    # Only reachable when the cap was hit with tool calls still pending.
    yield _sse("error", {"code": "tool_rounds_exhausted",
                         "message": f"Model still wanted tools after {MAX_TOOL_ROUNDS} rounds"})


@router.post("/stream")
def stream_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    return StreamingResponse(
        _events(payload, request.state.trace_id),
        media_type="text/event-stream",
        # Without these a proxy may buffer the whole answer and defeat the streaming.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
