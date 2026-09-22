"""Zhipu GLM provider over the OpenAI-compatible chat-completions shape.

No vendor SDK: httpx plus the documented wire format keeps the dependency surface small
and keeps DEVELOPMENT.md's "no vendor hardcoded in core" honest — this file is the only
place that knows the word "zhipu", and it satisfies contracts.llm.ModelProvider.

Credentials never appear in logs, exceptions or trace records.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from contracts.llm import (
    ChatMessage,
    Completion,
    ModelError,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)
from infrastructure.agent_config import AgentConfig, load_agent_config, resolve_credentials

ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("supplyagent.llm")

#: Bounded by design. EV-10 requires honouring Retry-After without a tight loop, and a
#: per-call retry budget is what stops a stalled provider from eating the run budget.
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 20.0


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float
    enabled: bool

    @property
    def redacted(self) -> dict[str, Any]:
        """Safe to log: proves configuration without disclosing the key."""
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model,
                "timeout_seconds": self.timeout_seconds, "enabled": self.enabled,
                "api_key": f"set({len(self.api_key)} chars)" if self.api_key else "missing"}


def load_settings(environ: dict[str, str] | None = None) -> LLMSettings:
    """Read the process configuration, or exactly the mapping given.

    An explicit mapping replaces the environment rather than layering on top of it, the
    same way resolve_credentials() treats its own. Merging .env into a caller-supplied
    dict would make a test's result depend on whoever ran it: fill in a key locally and
    an offline test that asserts "disabled by default" starts failing on your machine
    and nowhere else.
    """
    if environ is not None:
        values: dict[str, str | None] = dict(environ)
    else:
        values = {**dotenv_values(ROOT / ".env"), **os.environ}
    return LLMSettings(
        provider=values.get("SUPPLYAGENT_LLM_PROVIDER", "") or "",
        api_key=values.get("SUPPLYAGENT_LLM_API_KEY", "") or "",
        base_url=(values.get("SUPPLYAGENT_LLM_BASE_URL", "") or "").rstrip("/"),
        model=values.get("SUPPLYAGENT_LLM_MODEL", "") or "",
        timeout_seconds=float(values.get("SUPPLYAGENT_LLM_TIMEOUT_SECONDS", "60") or 60),
        enabled=str(values.get("SUPPLYAGENT_LLM_ENABLED", "false")).strip().lower() == "true",
    )


def _encode_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        payload["content"] = message.content
    if message.tool_calls:
        payload["tool_calls"] = [
            {"id": call.id, "type": "function",
             "function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _encode_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"type": "function",
            "function": {"name": tool.name, "description": tool.description,
                         "parameters": tool.parameters}}


def _decode_tool_calls(raw: list[dict[str, Any]] | None) -> tuple[ToolCall, ...]:
    if not raw:
        return ()
    calls = []
    for item in raw:
        function = item.get("function") or {}
        # arguments stays a raw string even when malformed; validation happens at the
        # tool boundary and the audit record must show what the model actually emitted.
        calls.append(ToolCall(id=str(item.get("id") or ""),
                              name=str(function.get("name") or ""),
                              arguments=function.get("arguments") or ""))
    return tuple(calls)


def _merge_tool_call_deltas(
    accumulator: dict[int, dict[str, str]],
    deltas: list[dict[str, Any]] | None,
) -> None:
    """Fold streamed tool-call fragments into whole calls.

    The id and name arrive once, the arguments arrive a few characters at a time. Both
    are appended rather than overwritten: a provider that repeats the name would
    otherwise truncate arguments that were already collected.
    """
    for delta in deltas or []:
        index = int(delta.get("index") or 0)
        call = accumulator.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if delta.get("id"):
            call["id"] = str(delta["id"])
        function = delta.get("function") or {}
        if function.get("name"):
            call["name"] = str(function["name"])
        if function.get("arguments"):
            call["arguments"] += str(function["arguments"])


def _retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if not header:
        return None
    try:
        return min(float(header), MAX_RETRY_WAIT_SECONDS)
    except ValueError:
        return None


class ZhipuProvider:
    """Implements contracts.llm.ModelProvider against Zhipu's GLM endpoint."""

    def __init__(self, settings: LLMSettings | None = None,
                 client: httpx.Client | None = None) -> None:
        self.settings = settings or load_settings()
        self.name = self.settings.provider or "zhipu"
        self._client = client
        self._owns_client = client is None

    def _require_ready(self) -> None:
        # Names the actual source of each value: credentials come from .env, the model
        # from config/agents/<worker>.yaml. A single generic message would send someone
        # looking in the wrong file (DECISIONS.md D07).
        missing = [name for name, value in (
            ("SUPPLYAGENT_LLM_API_KEY (.env)", self.settings.api_key),
            ("SUPPLYAGENT_LLM_BASE_URL (.env)", self.settings.base_url),
            ("model (config/agents/*.yaml)", self.settings.model),
        ) if not value]
        if missing:
            raise ModelError("provider_not_configured", f"Missing: {', '.join(missing)}")
        if not self.settings.enabled:
            # A real call costs money. AGENTS.md requires explicit authorisation, and an
            # env flag is where that authorisation is recorded.
            raise ModelError(
                "provider_disabled",
                "SUPPLYAGENT_LLM_ENABLED is not true; real model calls need explicit opt-in")

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> Completion:
        self._require_ready()
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [_encode_message(message) for message in messages],
            "temperature": temperature,
        }
        if tools:
            body["tools"] = [_encode_tool(tool) for tool in tools]
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format

        url = f"{self.settings.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.api_key}",
                   "Content-Type": "application/json"}
        started = time.monotonic()
        last_error: ModelError | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._http().post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                last_error = ModelError("timeout", f"Provider timed out: {exc!s}", retryable=True)
            except httpx.HTTPError as exc:
                last_error = ModelError("transport_error", f"Provider unreachable: {exc!s}",
                                        retryable=True)
            else:
                if response.status_code < 400:
                    return self._decode(response, started)
                last_error = self._classify(response)

            logger.warning("LLM call failed (attempt %s/%s, code=%s, trace_id=%s)",
                           attempt, MAX_ATTEMPTS, last_error.code, trace_id)
            if not last_error.retryable or attempt == MAX_ATTEMPTS:
                break
            # Honour the server's pacing when it gives one; otherwise back off. Never a
            # tight loop, and never longer than the cap (EV-10).
            time.sleep(last_error.retry_after if last_error.retry_after is not None
                       else min(2.0 ** (attempt - 1), MAX_RETRY_WAIT_SECONDS))

        assert last_error is not None
        raise last_error

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        trace_id: str | None = None,
    ) -> Iterator[StreamEvent]:
        """Server-sent chunks from the OpenAI-compatible /chat/completions shape.

        Retries are allowed only while nothing has been emitted yet. That line matters:
        a 429 or a refused connection happens before the user has seen a character, so
        retrying is invisible and correct; a break *mid-answer* is not retryable at all,
        because the second attempt would write a different answer over the first.
        """
        self._require_ready()
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [_encode_message(message) for message in messages],
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = [_encode_tool(tool) for tool in tools]
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        url = f"{self.settings.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.api_key}",
                   "Content-Type": "application/json"}
        started = time.monotonic()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            text_parts: list[str] = []
            # Keyed by the index the provider assigns, because a model may open several
            # calls at once and their argument fragments arrive interleaved.
            partial_calls: dict[int, dict[str, str]] = {}
            finish_reason = "unknown"
            model = self.settings.model
            usage = Usage()
            emitted = False
            error: ModelError | None = None

            try:
                with self._http().stream("POST", url, json=body, headers=headers) as response:
                    if response.status_code >= 400:
                        response.read()
                        error = self._classify(response)
                    else:
                        for line in response.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:"):].strip()
                            if data == "[DONE]":
                                break
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                raise ModelError(
                                    "malformed_response",
                                    "Provider sent a non-JSON stream chunk") from None
                            model = str(payload.get("model") or model)
                            if payload.get("usage"):
                                raw_usage = payload["usage"]
                                usage = Usage(
                                    prompt_tokens=raw_usage.get("prompt_tokens"),
                                    output_tokens=raw_usage.get("completion_tokens"))
                            for choice in payload.get("choices") or []:
                                if choice.get("finish_reason"):
                                    finish_reason = str(choice["finish_reason"])
                                delta = choice.get("delta") or {}
                                chunk = delta.get("content")
                                if chunk:
                                    text_parts.append(chunk)
                                    emitted = True
                                    yield StreamEvent("text", text=chunk)
                                _merge_tool_call_deltas(partial_calls, delta.get("tool_calls"))
            except httpx.TimeoutException as exc:
                error = ModelError("timeout", f"Provider timed out: {exc!s}", retryable=True)
            except httpx.HTTPError as exc:
                error = ModelError("transport_error", f"Stream broke: {exc!s}", retryable=True)

            if error is None:
                yield StreamEvent("done", completion=Completion(
                    content="".join(text_parts) or None,
                    tool_calls=tuple(
                        ToolCall(id=call["id"], name=call["name"], arguments=call["arguments"])
                        for _, call in sorted(partial_calls.items())),
                    model=model,
                    finish_reason=finish_reason,
                    usage=usage,
                    latency_ms=int((time.monotonic() - started) * 1000),
                ))
                return

            logger.warning("Stream attempt %s/%s failed (code=%s, emitted=%s, trace_id=%s)",
                           attempt, MAX_ATTEMPTS, error.code, emitted, trace_id)
            if emitted or not error.retryable or attempt == MAX_ATTEMPTS:
                raise error
            time.sleep(error.retry_after if error.retry_after is not None
                       else min(2.0 ** (attempt - 1), MAX_RETRY_WAIT_SECONDS))

    def _classify(self, response: httpx.Response) -> ModelError:
        status = response.status_code
        detail = response.text[:300]
        if status == 401:
            # Credentials are wrong or expired. Retrying with the same key cannot help.
            return ModelError("unauthorized", "Provider rejected the credential")
        if status == 403:
            # EV-11: never blind-retry a forbidden response.
            return ModelError("forbidden", "Provider denied access to this resource")
        if status == 429:
            # The provider's own wording travels with the error: 429 covers throttling,
            # quota and an overloaded model, and those need different responses from a
            # human. A generic "rate limited" sends people to check the wrong thing.
            return ModelError("rate_limited", f"Provider rate limit reached: {detail}",
                              retryable=True, retry_after=_retry_after(response))
        if status >= 500:
            return ModelError("provider_error", f"Provider server error {status}: {detail}",
                              retryable=True, retry_after=_retry_after(response))
        return ModelError("bad_request", f"Provider rejected the request ({status}): {detail}")

    def _decode(self, response: httpx.Response, started: float) -> Completion:
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ModelError("malformed_response", f"Provider returned non-JSON: {exc!s}") from None
        choices = payload.get("choices") or []
        if not choices:
            raise ModelError("malformed_response", "Provider returned no choices")
        message = choices[0].get("message") or {}
        usage = payload.get("usage") or {}
        return Completion(
            content=message.get("content"),
            tool_calls=_decode_tool_calls(message.get("tool_calls")),
            model=str(payload.get("model") or self.settings.model),
            finish_reason=str(choices[0].get("finish_reason") or "unknown"),
            usage=Usage(prompt_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens")),
            latency_ms=latency_ms,
        )


#: Provider name -> implementation. A worker's YAML picks by name, so adding a vendor
#: means adding an entry here and a credentials pair in .env, nothing else.
PROVIDERS: dict[str, type] = {"zhipu": ZhipuProvider}


def provider_for(
    worker: str,
    *,
    client: httpx.Client | None = None,
    environ: dict[str, str] | None = None,
) -> Any:
    """Build the provider this worker is configured to use.

    Model and sampling come from config/agents/<worker>.yaml; the key and endpoint come
    from .env, scoped by provider so two workers on different vendors stay separable.
    The returned provider carries `.config` so the caller can pass the worker's own
    temperature and max_tokens explicitly, keeping the llm_call audit row truthful about
    what was actually sent.
    """
    config: AgentConfig = load_agent_config(worker)
    implementation = PROVIDERS.get(config.provider)
    if implementation is None:
        raise ModelError(
            "provider_not_configured",
            f"{worker}.yaml names provider {config.provider!r}; "
            f"known providers: {', '.join(sorted(PROVIDERS))}")
    api_key, base_url = resolve_credentials(config.provider, environ)
    values = environ if environ is not None else {**dotenv_values(ROOT / ".env"), **os.environ}
    settings = LLMSettings(
        provider=config.provider,
        api_key=api_key,
        base_url=base_url,
        model=config.model,
        timeout_seconds=config.timeout_seconds,
        enabled=str(values.get("SUPPLYAGENT_LLM_ENABLED", "false")).strip().lower() == "true",
    )
    provider = implementation(settings=settings, client=client)
    provider.config = config
    return provider
