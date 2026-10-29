"""F50 fixed official free-model endpoint; Pi owns native message conversion."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

FREE_MODEL = "glm-4.7-flash"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
COMPAT = {
    "supportsStore": False,
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "supportsUsageInStreaming": False,
    "maxTokensField": "max_tokens",
    "supportsStrictMode": False,
    "zaiToolStream": False,
}


def validate_free_policy(policy):
    from supplyagent.service.gateway.gateway import GatewayError

    if (
        policy.model_id != FREE_MODEL
        or not 1024 <= policy.context_window <= 200000
        or policy.reasoning
        or any(
            (
                policy.input_price_micros_per_million,
                policy.output_price_micros_per_million,
                policy.cached_input_price_micros_per_million or 0,
            )
        )
    ):
        raise GatewayError("only the verified free GLM profile is enabled")


def effective_chat_request(policy, payload):
    validate_free_policy(policy)
    return {
        **payload,
        "model": FREE_MODEL,
        "max_tokens": policy.max_output_tokens,
        "thinking": {"type": "disabled"},
    }


class GlmOpenAICompatibleProvider:
    def __init__(self, credentials, client: httpx.AsyncClient | None = None):
        self.credentials, self.client = credentials, client

    async def stream(self, policy, payload) -> AsyncIterator[dict]:
        from supplyagent.service.gateway.gateway import GatewayError

        validate_free_policy(policy)
        if not policy.real_calls_enabled or not policy.credential_ref:
            raise GatewayError("real GLM calls disabled")
        key = self.credentials.resolve(policy.credential_ref)
        if not key:
            raise GatewayError("GLM credential unavailable")
        client = self.client or httpx.AsyncClient(timeout=60)
        try:
            async with client.stream(
                "POST",
                ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                json=effective_chat_request(policy, payload),
            ) as response:
                if response.status_code != 200:
                    raise GatewayError(
                        f"GLM request failed (HTTP {response.status_code})",
                        status_code=response.status_code,
                    )
                done = False
                async for line in response.aiter_lines():
                    if line == "data: [DONE]":
                        done = True
                        break
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except ValueError:
                        raise GatewayError("invalid GLM stream") from None
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise GatewayError("GLM stream rejected")
                    yield chunk
                if not done:
                    raise GatewayError("GLM stream interrupted")
        finally:
            if self.client is None:
                await client.aclose()
