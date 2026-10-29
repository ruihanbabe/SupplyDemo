"""F50 native GLM fixtures, free-only guards and the actual Pi converter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from test_product_api_gateway import _gateway, run_pi_fixture

from product_api.gateway import GatewayError
from product_api.glm import ENDPOINT, FREE_MODEL, GlmOpenAICompatibleProvider
from product_api.harness import CURRENT_TOOL_CALL, ToolDefinition
from product_api.specialist_runner import validate_no_arguments


class NativeGlmFixture:
    async def stream(self, policy, payload):
        messages = payload["messages"]
        results = [m for m in messages if m["role"] == "tool"]
        tool = payload["tools"][0]["function"]["name"]
        if not results:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_glm_fixture",
                        "type": "function",
                        "function": {"name": tool, "arguments": "{"},
                    }
                ]
            }
            yield {
                "id": "glm_fixture",
                "model": FREE_MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            delta = {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}
            reason = "tool_calls"
        else:
            task = next(m["content"] for m in messages if m["role"] == "user")
            if isinstance(task, list):
                task = "".join(p["text"] for p in task if p.get("type") == "text")
            role = json.loads(task)["role"]
            evidence = json.loads(results[-1]["content"])
            text = json.dumps(
                {
                    "role": role,
                    "complete": True,
                    "claims": [
                        {
                            "subject": "opportunity",
                            "claimType": "business_answer",
                            "conclusion": "GLM 合成回放，待业务审校。",
                            "quality": "needs_review",
                            "citations": [evidence["citation"]],
                            "unknowns": evidence.get("unknowns", []),
                            "conflicts": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )
            delta, reason = {"content": text}, "stop"
        yield {
            "id": "glm_fixture",
            "model": FREE_MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


def test_glm_provider_is_fixed_free_and_preserves_native_sse():
    class Credentials:
        def resolve(self, ref):
            return "SYNTHETIC-secret"

    async def scenario():
        _, accounting, _, _ = _gateway()
        policy = replace(
            accounting.policy_value,
            provider="zhipu",
            model_id=FREE_MODEL,
            credential_ref="ZHIPU",
            real_calls_enabled=True,
        )
        observed = []

        def respond(request):
            observed.append(request)
            assert str(request.url) == ENDPOINT
            body = json.loads(request.content)
            assert body["model"] == FREE_MODEL and body["thinking"] == {"type": "disabled"}
            assert body["max_tokens"] == policy.max_output_tokens
            assert request.headers["authorization"] == "Bearer SYNTHETIC-secret"
            return httpx.Response(
                200,
                text='data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\ndata: [DONE]\n\n',
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            adapter = GlmOpenAICompatibleProvider(Credentials(), client)
            chunks = [
                c
                async for c in adapter.stream(
                    policy, {"model": "glm-paid", "messages": [], "stream": True}
                )
            ]
            assert chunks[0]["usage"]["prompt_tokens"] == 2
            for invalid in (
                replace(policy, model_id="glm-5"),
                replace(policy, output_price_micros_per_million=1),
                replace(policy, real_calls_enabled=False),
            ):
                with pytest.raises(GatewayError):
                    _ = [c async for c in adapter.stream(invalid, {})]
            assert len(observed) == 1

    asyncio.run(scenario())


def test_actual_pi_glm_native_tool_messages_and_accounting():
    async def scenario():
        captured = []

        def evidence(arguments):
            captured.append(CURRENT_TOOL_CALL.get())
            return {
                "citation": {
                    "sourceKind": "intelligence_observation",
                    "sourceId": str(uuid4()),
                    "version": "1",
                    "locator": "payload",
                    "fragmentHash": "a" * 64,
                },
                "unknowns": ["synthetic"],
            }

        result, trace, accounting = await run_pi_fixture(
            NativeGlmFixture(),
            ToolDefinition("commercial.quote_validity", evidence, validate_no_arguments),
            '{"role":"quote_validity"}',
            glm=True,
        )
        assert result.output["role"] == "quote_validity"
        assert result.tool_calls == ("commercial.quote_validity",)
        assert accounting.settlements == [(10, 5, 0), (10, 5, 0)]
        assert captured[0]["modelCallKey"] == accounting.calls[0]
        assert captured[0]["toolCallId"] == "call_glm_fixture"
        assert accounting.exchanges[-1][1]["messages"][-1]["tool_call_id"] == "call_glm_fixture"
        assert any(e["metadata"].get("name") == "commercial.quote_validity" for e in trace.events)

    asyncio.run(scenario())
