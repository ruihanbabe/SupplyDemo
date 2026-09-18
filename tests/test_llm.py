"""Offline provider tests: wire shape, error classification, retry discipline, secrecy.

No network and no real key. httpx.MockTransport stands in for the provider so the
failure modes that matter (EV-10 rate limiting, EV-11 auth) can be exercised
deterministically instead of hoped for.
"""
from __future__ import annotations

import json

import httpx
import pytest

from contracts.llm import ChatMessage, ModelError, ToolCall, ToolSpec
from infrastructure.llm import MAX_ATTEMPTS, LLMSettings, ZhipuProvider, load_settings

SECRET = "sk-super-secret-key-value-do-not-leak"


def settings(**overrides) -> LLMSettings:
    base = {"provider": "zhipu", "api_key": SECRET,
            "base_url": "https://provider.invalid/api/paas/v4", "model": "glm-4-flash",
            "timeout_seconds": 5.0, "enabled": True}
    return LLMSettings(**{**base, **overrides})


def provider(handler, **overrides) -> ZhipuProvider:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ZhipuProvider(settings(**overrides), client=client)


def ok_payload(**overrides):
    payload = {"model": "glm-4-flash",
               "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    payload.update(overrides)
    return payload


def user(text="hi"):
    return [ChatMessage(role="user", content=text)]


# ---------- 凭据保密 ----------

def test_redacted_settings_never_expose_the_key():
    rendered = json.dumps(settings().redacted)
    assert SECRET not in rendered
    assert "set(" in rendered


def test_key_never_appears_in_errors():
    def handler(request):
        return httpx.Response(400, text="bad request detail")

    with pytest.raises(ModelError) as caught:
        provider(handler).complete(user())
    assert SECRET not in str(caught.value)
    assert SECRET not in repr(caught.value)


def test_key_travels_only_in_the_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=ok_payload())

    provider(handler).complete(user())
    assert seen["auth"] == f"Bearer {SECRET}"
    assert SECRET not in seen["body"]


# ---------- 启用门与配置 ----------

def test_disabled_provider_refuses_to_call():
    """A real call costs money; AGENTS.md requires explicit authorisation."""
    called = []

    def handler(request):
        called.append(1)
        return httpx.Response(200, json=ok_payload())

    with pytest.raises(ModelError) as caught:
        provider(handler, enabled=False).complete(user())
    assert caught.value.code == "provider_disabled"
    assert not called, "disabled provider must not reach the network"


@pytest.mark.parametrize("missing", ["api_key", "base_url", "model"])
def test_incomplete_configuration_is_named(missing):
    with pytest.raises(ModelError) as caught:
        provider(lambda r: httpx.Response(200), **{missing: ""}).complete(user())
    assert caught.value.code == "provider_not_configured"
    # The message names the file that actually holds each value, so match
    # case-insensitively: credentials are env vars, model is YAML.
    assert missing.lower() in str(caught.value).lower()


def test_load_settings_defaults_to_disabled():
    loaded = load_settings({"SUPPLYAGENT_LLM_PROVIDER": "zhipu"})
    assert loaded.enabled is False


def test_load_settings_enables_only_on_explicit_true():
    assert load_settings({"SUPPLYAGENT_LLM_ENABLED": "TRUE"}).enabled is True
    assert load_settings({"SUPPLYAGENT_LLM_ENABLED": "1"}).enabled is False
    assert load_settings({"SUPPLYAGENT_LLM_ENABLED": "yes"}).enabled is False


# ---------- 线格式 ----------

def test_successful_completion_is_decoded():
    result = provider(lambda r: httpx.Response(200, json=ok_payload())).complete(user())
    assert result.content == "hello"
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.latency_ms >= 0
    assert result.wants_tools is False


def test_tools_are_encoded_in_the_documented_shape():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_payload())

    tool = ToolSpec(name="search_supplier_stock", description="Find stock",
                    parameters={"type": "object", "properties": {"mpn": {"type": "string"}},
                                "required": ["mpn"]})
    provider(handler).complete(user(), tools=[tool])
    assert seen["tools"] == [{"type": "function", "function": {
        "name": "search_supplier_stock", "description": "Find stock",
        "parameters": tool.parameters}}]
    assert seen["temperature"] == 0.0


def test_tool_calls_are_decoded():
    payload = ok_payload(choices=[{"message": {"content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "search_supplier_stock", "arguments": '{"mpn":"IRFZ44N"}'}}]},
        "finish_reason": "tool_calls"}])
    result = provider(lambda r: httpx.Response(200, json=payload)).complete(user())
    assert result.wants_tools
    assert result.tool_calls[0].name == "search_supplier_stock"
    assert result.tool_calls[0].arguments == '{"mpn":"IRFZ44N"}'


def test_malformed_tool_arguments_are_preserved_verbatim():
    """Model output is untrusted input. The audit record must show what it really emitted."""
    payload = ok_payload(choices=[{"message": {"tool_calls": [
        {"id": "c1", "function": {"name": "x", "arguments": "{not json"}}]},
        "finish_reason": "tool_calls"}])
    result = provider(lambda r: httpx.Response(200, json=payload)).complete(user())
    assert result.tool_calls[0].arguments == "{not json"


def test_tool_result_message_carries_its_call_id():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=ok_payload())

    provider(handler).complete([
        ChatMessage(role="assistant", tool_calls=(ToolCall("c1", "x", "{}"),)),
        ChatMessage(role="tool", content='{"ok":true}', tool_call_id="c1"),
    ])
    assert seen["messages"][1] == {"role": "tool", "content": '{"ok":true}', "tool_call_id": "c1"}


# ---------- 错误分类与重试纪律 ----------

def attempts_for(status, *, headers=None, monkeypatch=None):
    """Counts HTTP attempts separately from sleeps: a 1.0s backoff equals 1 in Python."""
    calls, waits = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers=headers or {}, text="detail")

    if monkeypatch is not None:
        monkeypatch.setattr("infrastructure.llm.time.sleep", waits.append)
    with pytest.raises(ModelError) as caught:
        provider(handler).complete(user())
    return caught.value, calls


def test_unauthorized_is_not_retried():
    error, tries = attempts_for(401)
    assert error.code == "unauthorized"
    assert error.retryable is False
    assert len(tries) == 1


def test_forbidden_is_not_retried():
    """EV-11: a 403 means the permission is missing, not that the call was unlucky."""
    error, tries = attempts_for(403)
    assert error.code == "forbidden"
    assert error.retryable is False
    assert len(tries) == 1


def test_rate_limit_is_retried_within_budget(monkeypatch):
    error, tries = attempts_for(429, monkeypatch=monkeypatch)
    assert error.code == "rate_limited"
    assert error.retryable is True
    assert len(tries) == MAX_ATTEMPTS, "retries must stop at the attempt budget"


def test_rate_limit_honours_retry_after(monkeypatch):
    waits = []
    monkeypatch.setattr("infrastructure.llm.time.sleep", waits.append)

    def handler(request):
        return httpx.Response(429, headers={"retry-after": "7"}, text="slow down")

    with pytest.raises(ModelError):
        provider(handler).complete(user())
    # EV-10: obey the server's pacing rather than spinning.
    assert waits == [7.0, 7.0]


def test_absurd_retry_after_is_capped(monkeypatch):
    waits = []
    monkeypatch.setattr("infrastructure.llm.time.sleep", waits.append)

    def handler(request):
        return httpx.Response(429, headers={"retry-after": "99999"}, text="x")

    with pytest.raises(ModelError):
        provider(handler).complete(user())
    assert all(wait <= 20.0 for wait in waits)


def test_server_error_is_retryable(monkeypatch):
    error, tries = attempts_for(503, monkeypatch=monkeypatch)
    assert error.code == "provider_error"
    assert len(tries) == MAX_ATTEMPTS


def test_timeout_is_retryable_and_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr("infrastructure.llm.time.sleep", lambda s: None)

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ModelError) as caught:
        provider(handler).complete(user())
    assert caught.value.code == "timeout"
    assert caught.value.retryable is True
    assert len(calls) == MAX_ATTEMPTS


def test_empty_choices_is_an_error_not_an_empty_answer():
    """External failure must never be disguised as a successful empty result."""
    with pytest.raises(ModelError) as caught:
        provider(lambda r: httpx.Response(200, json={"choices": []})).complete(user())
    assert caught.value.code == "malformed_response"


def test_non_json_body_is_an_error():
    with pytest.raises(ModelError) as caught:
        provider(lambda r: httpx.Response(200, text="<html>gateway</html>")).complete(user())
    assert caught.value.code == "malformed_response"
