"""Deterministic replay of recorded model answers.

Addressed by request content, never by call order. That distinction is the whole design:
a run that is interrupted and resumed re-enters a node and issues the *same* request
again. Order-indexed playback would hand it the next recording instead of the same one,
so recovery would diverge from the original run and the replay would be useless as a
regression baseline (F10, F20).

A missing recording is an error. Replay never improvises an answer, because an
improvised answer cannot serve as a baseline for anything.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts import errors
from contracts.llm import (
    ModelCapabilities,
    ModelError,
    ModelRequest,
    ModelResult,
    StreamEvent,
    ToolCall,
    Usage,
    negotiate,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_DIR = ROOT / "fixtures" / "model"

#: How a streamed answer is cut up when a recording holds only the final text. Replay
#: reproduces the recorded chunks when it has them; this is the fallback.
_FALLBACK_CHUNK = 12


def request_digest(request: ModelRequest) -> str:
    """Stable key for one logical request.

    Only what changes the answer takes part. call_id, trace_id, timeouts and
    context_bundle_id are excluded: they differ on every run, and including them would
    mean a resumed run never matches its own recording. `stream` is excluded too, so the
    same question asked either way resolves to one recording.
    """
    canonical = {
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "parallel_tool_calls": request.parallel_tool_calls,
        "output_schema": request.output_schema,
        "messages": [
            {"role": message.role, "content": message.content,
             "tool_call_id": message.tool_call_id,
             "tool_calls": [asdict(call) for call in message.tool_calls]}
            for message in request.messages],
        "tools": [asdict(tool) for tool in request.tools],
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _result_from(recording: dict[str, Any], request: ModelRequest, backend: str) -> ModelResult:
    raw = recording.get("result") or {}
    usage = raw.get("usage") or {}
    return ModelResult(
        content=raw.get("content"),
        tool_calls=tuple(ToolCall(id=call["id"], name=call["name"], arguments=call["arguments"])
                         for call in raw.get("tool_calls") or []),
        structured_output=raw.get("structured_output"),
        model=str(raw.get("model") or request.model),
        backend=backend,
        finish_reason=str(raw.get("finish_reason") or "stop"),
        usage=Usage(prompt_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("output_tokens")),
        # Zero, not the recorded value: no time was spent here, and reporting the
        # original latency would put a measurement in the audit trail that never
        # happened. Zero is also constant, which keeps replayed runs comparable.
        latency_ms=0,
        call_id=request.call_id,
    )


class ReplayBackend:
    """contracts.llm.ModelBackend backed by recordings on disk."""

    name = "replay"

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = fixture_dir or DEFAULT_FIXTURE_DIR

    def capabilities(self) -> ModelCapabilities:
        # Replay can reproduce whatever was recorded, so it claims the capabilities a
        # recording may contain. It cannot cancel, because there is nothing in flight.
        return ModelCapabilities(
            native_tool_calling=True,
            parallel_tool_calls=True,
            structured_output=True,
            streaming=True,
            cancellation=False,
            usage_reporting=True,
            vision_input=False,
            max_context_tokens=None,
        )

    def _load(self, request: ModelRequest) -> dict[str, Any]:
        digest = request_digest(request)
        path = self.fixture_dir / f"{digest}.json"
        if not path.exists():
            raise ModelError(
                errors.FIXTURE_MISSING,
                f"No recording for this request (digest {digest[:12]}…). "
                f"Expected {path}. Record it with RecordingBackend rather than "
                f"inventing an answer.")
        return json.loads(path.read_text(encoding="utf-8"))

    def complete(self, request: ModelRequest) -> ModelResult:
        negotiate(request, self.capabilities())
        return _result_from(self._load(request), request, self.name)

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        negotiate(request, self.capabilities())
        recording = self._load(request)
        result = _result_from(recording, request, self.name)
        chunks = recording.get("chunks")
        if not chunks:
            text = result.content or ""
            chunks = [text[i:i + _FALLBACK_CHUNK] for i in range(0, len(text), _FALLBACK_CHUNK)]
        for chunk in chunks:
            if chunk:
                yield StreamEvent("text", text=chunk)
        yield StreamEvent("done", result=result)


class RecordingBackend:
    """Delegates to a real backend and writes what came back.

    Kept deliberately thin and separate from ReplayBackend: recording touches the
    network and costs money, replay must never do either. Nothing records implicitly —
    a caller has to wrap a backend on purpose.
    """

    def __init__(self, inner: Any, fixture_dir: Path | None = None) -> None:
        self.inner = inner
        self.name = getattr(inner, "name", "recording")
        self.fixture_dir = fixture_dir or DEFAULT_FIXTURE_DIR

    def capabilities(self) -> ModelCapabilities:
        return self.inner.capabilities()

    def _write(self, request: ModelRequest, result: ModelResult, chunks: list[str]) -> None:
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        digest = request_digest(request)
        payload = {
            "digest": digest,
            "recorded_at": datetime.now(UTC).isoformat(),
            "backend": result.backend or self.name,
            "model": result.model,
            "chunks": chunks,
            "result": {
                "content": result.content,
                "tool_calls": [asdict(call) for call in result.tool_calls],
                "structured_output": result.structured_output,
                "model": result.model,
                "finish_reason": result.finish_reason,
                "usage": {"prompt_tokens": result.usage.prompt_tokens,
                          "output_tokens": result.usage.output_tokens},
                "recorded_latency_ms": result.latency_ms,
            },
        }
        (self.fixture_dir / f"{digest}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def complete(self, request: ModelRequest) -> ModelResult:
        result = self.inner.complete(request)
        self._write(request, result, [])
        return result

    def stream(self, request: ModelRequest) -> Iterator[StreamEvent]:
        chunks: list[str] = []
        for event in self.inner.stream(request):
            if event.kind == "text":
                chunks.append(event.text)
            elif event.kind == "done" and event.result is not None:
                self._write(request, event.result, chunks)
            yield event
