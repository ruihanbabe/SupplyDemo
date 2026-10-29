"""Explicit opt-in live smoke: free GLM only, synthetic evidence, at most 3 requests."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from dotenv import load_dotenv
from test_product_api_gateway import run_pi_fixture

from product_api.gateway import EnvironmentCredentialResolver, GatewayError
from product_api.glm import GlmOpenAICompatibleProvider
from product_api.harness import ToolDefinition
from product_api.intelligence import INSTRUCTIONS
from product_api.specialist_runner import validate_no_arguments


@pytest.mark.model_live
def test_free_glm_real_tool_roundtrip():
    if os.getenv("SUPPLYAGENT_GLM_LIVE") != "1":
        pytest.skip("explicit live GLM authorization required")
    load_dotenv(".env")
    asyncio.run(scenario())


async def scenario():
    class BoundedProvider(GlmOpenAICompatibleProvider):
        requests = 0

        async def stream(self, policy, payload):
            if self.requests >= 3:
                raise GatewayError("live smoke request limit reached")
            self.requests += 1
            async for chunk in super().stream(policy, payload):
                yield chunk

    provider = BoundedProvider(EnvironmentCredentialResolver())
    citation = {
        "sourceKind": "intelligence_observation",
        "sourceId": str(uuid4()),
        "version": "1",
        "locator": "payload",
        "fragmentHash": "a" * 64,
    }
    captured = []

    def evidence(arguments):
        captured.append(arguments)
        return {
            "asOf": "2026-10-23T01:00:00Z",
            "quotes": [
                {
                    "reference": "SYNTHETIC-Q1",
                    "validity": "expired",
                    "stock_reserved": False,
                    "confirmed_arrival_on": None,
                }
            ],
            "unknowns": ["SYNTHETIC：未锁货，未确认到货。"],
            "citation": citation,
        }

    result, trace, accounting = await run_pi_fixture(
        provider,
        ToolDefinition("commercial.quote_validity", evidence, validate_no_arguments),
        '{"mission":"opportunity_intelligence","role":"quote_validity","question":"仅核验合成报价的时效，保留未知。"}',
        glm=True,
        timeout_seconds=60,
        instructions=INSTRUCTIONS,
    )
    assert result.status == "completed" and captured
    assert result.output is not None and result.output["role"] == "quote_validity"
    assert result.output["complete"] is True
    assert result.output["claims"] and all(
        citation in c["citations"] for c in result.output["claims"]
    )
    assert all(tokens[2] == 0 for tokens in accounting.settlements)
    assert 2 <= provider.requests <= 3 and len(accounting.settlements) == provider.requests
    # Output only bounded metadata; never credentials, prompts or raw model messages.
    print(
        {
            "provider": "zhipu",
            "model": "glm-4.7-flash",
            "requests": provider.requests,
            "toolCalls": len(result.tool_calls),
            "usage": accounting.settlements,
            "protocol": "chat-completions",
            "structuredOutput": True,
            "traceEvents": len(trace.events),
        }
    )
