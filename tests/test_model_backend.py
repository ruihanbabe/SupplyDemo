"""F10: one contract, two backends, and a replay key that survives a restart.

The point of these tests is not that each backend works. It is that they are
*interchangeable*: the same ModelRequest produces the same normalised ModelResult
whichever one answers it, and a capability neither of them has is refused rather than
quietly dropped (DECISIONS.md D05).
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from contracts.errors import CAPABILITY_UNSUPPORTED, FIXTURE_MISSING
from contracts.llm import (
    ChatMessage,
    ModelCapabilities,
    ModelError,
    ModelRequest,
    ToolCall,
    ToolSpec,
    negotiate,
)
from infrastructure.llm import LLMSettings, OpenAICompatibleBackend
from infrastructure.replay import RecordingBackend, ReplayBackend, request_digest

ROOT = Path(__file__).resolve().parents[1]

TOOL = ToolSpec(name="list_projects", description="List projects",
                parameters={"type": "object", "properties": {}, "required": []})


def make_request(**overrides) -> ModelRequest:
    base = ModelRequest(call_id="call-1", model="test-model",
                        messages=(ChatMessage(role="user", content="有哪些项目"),))
    return replace(base, **overrides)


def wire_backend(payload: dict) -> OpenAICompatibleBackend:
    settings = LLMSettings(provider="zhipu", api_key="k", base_url="https://provider.invalid",
                           model="test-model", timeout_seconds=5.0, enabled=True)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    return OpenAICompatibleBackend(settings, client=client)


ANSWER = {"model": "test-model", "finish_reason": "stop", "content": "共 5 个项目。"}


def wire_payload() -> dict:
    return {"model": ANSWER["model"],
            "choices": [{"message": {"content": ANSWER["content"]},
                         "finish_reason": ANSWER["finish_reason"]}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6}}


def write_fixture(directory: Path, request: ModelRequest, **result) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_digest(request)}.json"
    path.write_text(json.dumps({"digest": request_digest(request), "result": result},
                               ensure_ascii=False), encoding="utf-8")
    return path


# ---------- 能力协商：缺失必须显式拒绝 ----------

NOTHING = ModelCapabilities()
EVERYTHING = ModelCapabilities(native_tool_calling=True, parallel_tool_calls=True,
                               structured_output=True, streaming=True)


@pytest.mark.parametrize("overrides,missing", [
    ({"tools": (TOOL,)}, "native_tool_calling"),
    ({"tools": (TOOL,), "parallel_tool_calls": True}, "parallel_tool_calls"),
    ({"output_schema": {"type": "object"}}, "structured_output"),
    ({"stream": True}, "streaming"),
])
def test_missing_capability_is_refused_not_dropped(overrides, missing):
    """D05: a backend that cannot honour a constraint refuses; it never removes it.

    Silently dropping an output schema would return free text where the caller expects
    an object, and the failure would surface far from its cause.
    """
    with pytest.raises(ModelError) as caught:
        negotiate(make_request(**overrides), NOTHING)
    assert caught.value.code == CAPABILITY_UNSUPPORTED
    assert missing in str(caught.value)


def test_capable_backend_accepts_the_same_request():
    negotiate(make_request(tools=(TOOL,), parallel_tool_calls=True, stream=True,
                           output_schema={"type": "object"}), EVERYTHING)


class _Incapable(OpenAICompatibleBackend):
    """The same wire implementation, declaring it can do none of it."""

    def capabilities(self) -> ModelCapabilities:
        return NOTHING


def test_negotiation_runs_before_config_and_before_the_network():
    """A refusal must cost nothing: no key needed, no request sent.

    Order matters. Checking credentials first would report a missing key for a request
    that was never sendable anyway, and send someone to edit .env over a problem that
    lives in the request.
    """
    unreachable = _Incapable(
        LLMSettings(provider="zhipu", api_key="", base_url="", model="m",
                    timeout_seconds=1.0, enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: pytest.fail("negotiation let a refused request reach the wire"))))
    with pytest.raises(ModelError) as caught:
        unreachable.complete(make_request(output_schema={"type": "object"}))
    assert caught.value.code == CAPABILITY_UNSUPPORTED


# ---------- replay 键：按内容寻址，不按调用序号 ----------

def test_digest_ignores_per_run_identity():
    """The same question asked in a resumed run must hit the same recording.

    call_id, trace_id and the streaming choice all differ between the original run and
    the one that resumes it. If any of them entered the key, recovery would miss its own
    recording — the failure mode features.json warns about.
    """
    first = make_request()
    resumed = make_request(call_id="call-99", trace_id="other", stream=True,
                           context_bundle_id="bundle-7", timeout_seconds=99.0)
    assert request_digest(first) == request_digest(resumed)


def test_digest_changes_when_the_question_changes():
    assert request_digest(make_request()) != request_digest(
        make_request(messages=(ChatMessage(role="user", content="别的问题"),)))
    assert request_digest(make_request()) != request_digest(make_request(model="other-model"))
    assert request_digest(make_request()) != request_digest(make_request(tools=(TOOL,)))


def test_digest_is_stable_across_processes():
    """EV-30: byte-stable replay. A key that depends on process state is not a key."""
    script = (
        "import sys; sys.path.insert(0, 'src');"
        "from contracts.llm import ChatMessage;"
        "from contracts.llm import ModelRequest;"
        "from infrastructure.replay import request_digest;"
        "print(request_digest(ModelRequest(call_id='x', model='test-model',"
        "messages=(ChatMessage(role='user', content='有哪些项目'),))))")
    out = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == request_digest(make_request())


# ---------- replay 行为 ----------

def test_missing_recording_is_an_error_not_an_invention(tmp_path):
    with pytest.raises(ModelError) as caught:
        ReplayBackend(tmp_path).complete(make_request())
    assert caught.value.code == FIXTURE_MISSING
    assert request_digest(make_request())[:12] in str(caught.value)


def test_replay_reports_zero_latency(tmp_path):
    """Replaying the recorded latency would put a measurement in the audit trail that
    never happened, and would make two replayed runs incomparable."""
    request = make_request()
    write_fixture(tmp_path, request, content="x", finish_reason="stop",
                  recorded_latency_ms=4321)
    assert ReplayBackend(tmp_path).complete(request).latency_ms == 0


def test_replay_returns_recorded_tool_calls(tmp_path):
    request = make_request()
    write_fixture(tmp_path, request, content=None, finish_reason="tool_calls",
                  tool_calls=[{"id": "c1", "name": "list_projects", "arguments": "{}"}])
    result = ReplayBackend(tmp_path).complete(request)
    assert result.wants_tools
    assert result.tool_calls == (ToolCall("c1", "list_projects", "{}"),)


def test_recording_then_replaying_reproduces_the_stream(tmp_path):
    """Record once from the wire, replay forever without it."""
    request = make_request(stream=True)
    recorder = RecordingBackend(wire_backend(wire_payload()), fixture_dir=tmp_path)
    # The wire backend is not a streaming transport here, so record via complete() and
    # let replay chunk the text: what matters is that the answer survives the round trip.
    recorded = recorder.complete(request)

    replayed_events = list(ReplayBackend(tmp_path).stream(request))
    assert replayed_events[-1].kind == "done"
    replayed = replayed_events[-1].result
    assert "".join(e.text for e in replayed_events if e.kind == "text") == recorded.content
    assert replayed.content == recorded.content
    assert replayed.finish_reason == recorded.finish_reason
    assert replayed.usage.output_tokens == recorded.usage.output_tokens


# ---------- 同一契约，两个后端 ----------

@pytest.fixture(params=["wire", "replay"])
def backend(request, tmp_path):
    """Both backends, answering the same question the same way."""
    if request.param == "wire":
        return wire_backend(wire_payload())
    write_fixture(tmp_path, make_request(), content=ANSWER["content"],
                  finish_reason=ANSWER["finish_reason"], model=ANSWER["model"],
                  usage={"prompt_tokens": 12, "output_tokens": 6})
    return ReplayBackend(tmp_path)


def test_both_backends_normalise_the_same_answer(backend):
    result = backend.complete(make_request())
    assert result.content == ANSWER["content"]
    assert result.finish_reason == ANSWER["finish_reason"]
    assert result.model == ANSWER["model"]
    assert result.usage.output_tokens == 6
    assert result.call_id == "call-1"
    # The backend names itself so a cross-backend comparison can group by it. Same
    # model, different backend, is a real case (data-model.md llm_call.backend).
    assert result.backend == backend.name


def test_both_backends_declare_their_capabilities(backend):
    capabilities = backend.capabilities()
    assert capabilities.native_tool_calling is True
    assert capabilities.streaming is True
    # Neither can cancel an in-flight call yet; claiming otherwise would let the runtime
    # build a timeout path that silently does nothing.
    assert capabilities.cancellation is False
