"""F34 HTTP capability, Replay stream and real Pi tool-loop boundary."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from psycopg import OperationalError

from product_api.gateway import (
    CapabilitySigner,
    GatewayError,
    ModelCapability,
    ModelGateway,
    ModelPolicy,
    OpenAIResponsesProvider,
    ReplayResponsesProvider,
    create_model_gateway_app,
    policy_hash,
)
from product_api.harness import (
    Harness,
    HarnessContext,
    MemoryTraceSink,
    PiRuntimeAdapter,
    ToolDefinition,
    ToolRegistry,
)

SIDECAR = Path(__file__).resolve().parents[1] / "pi_sidecar" / "f42_protocol_sidecar.mjs"


class MemoryAccounting:
    def __init__(self, policy: ModelPolicy) -> None:
        self.policy_value = policy
        self.calls: list[str] = []
        self.settlements: list[tuple[int, int, int]] = []
        self.exchanges = []
        self.finished = []

    async def policy(self, scope):
        return self.policy_value

    async def record(self, scope, reservation, key, payload, policy):
        self.exchanges.append((key, payload))

    async def finish(self, scope, reservation, response, state):
        self.finished.append((response, state))

    async def reserve(self, scope: ModelCapability, call_key: str, input_digest: str):
        assert scope.policy_id == self.policy_value.id and len(input_digest) == 64
        self.calls.append(call_key)
        return self.policy_value, uuid4()

    async def settle(
        self,
        scope: ModelCapability,
        reservation_id,
        output_digest: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        outcome="success",
        response=None,
        state="completed",
    ) -> None:
        assert (
            scope.policy_id == self.policy_value.id and reservation_id and len(output_digest) == 64
        )
        self.settlements.append((input_tokens, output_tokens, cost_micros))
        if response is not None:
            self.finished.append((response, state))


def _gateway():
    policy = ModelPolicy(uuid4(), uuid4(), "replay", "fixed-v1", None, False, 1000, 600, 0, 0, 128)
    scope = ModelCapability(
        uuid4(), policy.organization_id, uuid4(), policy.id, int(time.time()) + 60
    )
    accounting = MemoryAccounting(policy)
    signer = CapabilitySigner(b"t" * 32)
    gateway = ModelGateway(signer, accounting, {"replay": ReplayResponsesProvider()})
    return gateway, accounting, scope, signer.issue(scope)


@pytest.mark.parametrize(
    "failure", [None, OSError("secret-dsn"), OperationalError("secret-sql"), TimeoutError()]
)
def test_gateway_health_and_readiness_do_not_call_provider_or_expose_errors(failure):
    async def scenario():
        gateway, accounting, _, _ = _gateway()

        async def probe():
            if failure is not None:
                raise failure

        accounting.ready = probe
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_model_gateway_app(gateway)),
            base_url="http://gateway",
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            response = await client.get("/ready")
            assert response.status_code == (200 if failure is None else 503)
            assert response.json() == {"status": "ready" if failure is None else "unavailable"}
        assert not accounting.calls and not accounting.exchanges and not accounting.settlements

    asyncio.run(scenario())


def test_http_capability_rejects_tampering_and_unregistered_tool() -> None:
    async def scenario() -> None:
        gateway, accounting, _scope, token = _gateway()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_model_gateway_app(gateway)),
            base_url="http://gateway",
        )
        payload = {
            "model": "ignored",
            "input": [{"role": "user", "content": "task"}],
            "stream": True,
        }
        headers = {
            "authorization": f"Bearer {token}",
            "x-supplyagent-call-key": f"{uuid4()}-1",
            "x-supplyagent-policy-hash": policy_hash(accounting.policy_value),
        }
        try:
            assert (
                await client.post(
                    "/v1/responses",
                    json=payload,
                    headers={**headers, "authorization": f"Bearer {token}x"},
                )
            ).status_code == 403
            bad = {**payload, "tools": [{"type": "function", "name": "shell_exec"}]}
            assert (
                await client.post("/v1/responses", json=bad, headers=headers)
            ).status_code == 403
            response = await client.post("/v1/responses", json=payload, headers=headers)
            assert response.status_code == 200 and "response.completed" in response.text
            assert accounting.calls == [headers["x-supplyagent-call-key"]]
            assert accounting.settlements == [(0, 0, 0)]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_real_pi_uses_scoped_http_gateway_for_two_tool_calls() -> None:
    async def scenario() -> None:
        gateway, accounting, scope, token = _gateway()
        app = create_model_gateway_app(gateway)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        listening = socket.socket()
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        port = listening.getsockname()[1]
        server_task = asyncio.create_task(server.serve(sockets=[listening]))
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            trace = MemoryTraceSink()
            harness = Harness(
                PiRuntimeAdapter(
                    ("node", str(SIDECAR)),
                    gateway_url=f"http://127.0.0.1:{port}/v1",
                    gateway_capability=token,
                ),
                ToolRegistry(
                    (
                        ToolDefinition(
                            "supply.impact", lambda arguments: {"part": arguments["partNumber"]}
                        ),
                    )
                ),
            )
            result = await harness.execute(
                HarnessContext(scope.organization_id, scope.run_id, None, "Investigate PART-A"),
                trace,
            )
            assert result.status == "completed"
            assert result.tool_calls == ("supply.impact", "supply.impact")
            assert len(accounting.calls) == len(accounting.settlements) == 3
            assert all(item == (0, 0, 0) for item in accounting.settlements)
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)

    asyncio.run(scenario())


def test_provider_adapter_uses_server_resolved_model_and_credential() -> None:
    class Resolver:
        def resolve(self, reference: str) -> str:
            assert reference == "vault-ref-1"
            return "test-provider-key"

    async def scenario() -> None:
        observed = []

        def answer(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                text='event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed","usage":{"input_tokens":2,"output_tokens":3}}}\n\n',
                headers={"content-type": "text/event-stream"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        policy = ModelPolicy(
            uuid4(), uuid4(), "openai", "server-model", "vault-ref-1", True, 1000, 600, 1, 2, 50
        )
        provider = OpenAIResponsesProvider(Resolver(), client)
        try:
            chunks = [
                chunk
                async for chunk in provider.stream(
                    policy, {"model": "client-forged", "input": [], "stream": True}
                )
            ]
            assert len(chunks) == 1 and chunks[0]["response"]["usage"]["input_tokens"] == 2
            assert len(observed) == 1
            assert observed[0].headers["authorization"] == "Bearer test-provider-key"
            assert observed[0].url.host == "api.openai.com"
            assert b'"model":"server-model"' in observed[0].content
            assert b'"max_output_tokens":50' in observed[0].content
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_integer_cost_and_unknown_usage_preserve_budget_reservation() -> None:
    class ChargedProvider:
        async def stream(self, _policy, _payload):
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_charge",
                    "status": "completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                },
            }

    class InterruptedProvider:
        async def stream(self, _policy, _payload):
            yield {"type": "response.output_text.delta", "delta": "partial"}

    async def scenario() -> None:
        gateway, accounting, scope, _token = _gateway()
        accounting.policy_value = ModelPolicy(
            accounting.policy_value.id,
            scope.organization_id,
            "replay",
            "fixed-v1",
            None,
            False,
            1000,
            600,
            500_000,
            250_000,
            128,
        )
        gateway.providers["replay"] = ChargedProvider()
        policy, reservation = await gateway.prepare(scope, "charged", {}, "a" * 64)
        assert (
            b"response.completed"
            in [line async for line in gateway.stream(scope, policy, reservation, {})][-1]
        )
        assert accounting.settlements == [(3, 2, 2)]
        gateway.providers["replay"] = InterruptedProvider()
        policy, reservation = await gateway.prepare(scope, "interrupted", {}, "b" * 64)
        try:
            _ = [line async for line in gateway.stream(scope, policy, reservation, {})]
            assert False, "unknown usage must fail"
        except GatewayError:
            pass
        assert accounting.settlements == [(3, 2, 2)]

    asyncio.run(scenario())


async def run_pi_fixture(
    provider,
    definition,
    task,
    *,
    glm=False,
    timeout_seconds=5,
    instructions=None,
    protocol_version=1,
    scheduler_policy=None,
    cancel_event=None,
):
    gateway, accounting, scope, token = _gateway()
    if glm:
        from dataclasses import replace

        from product_api.glm import FREE_MODEL

        accounting.policy_value = replace(
            accounting.policy_value,
            provider="zhipu",
            model_id=FREE_MODEL,
            context_window=200000,
            real_calls_enabled=True,
            credential_ref="ZHIPU",
            max_output_tokens=1024,
        )
        gateway.providers = {"zhipu": provider}
    else:
        gateway.providers["replay"] = provider
    server = uvicorn.Server(
        uvicorn.Config(create_model_gateway_app(gateway), log_level="error", lifespan="off")
    )
    listening = socket.socket()
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    port = listening.getsockname()[1]
    serving = asyncio.create_task(server.serve(sockets=[listening]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        trace = MemoryTraceSink()
        result = await Harness(
            PiRuntimeAdapter(
                ("node", str(SIDECAR)),
                gateway_url=f"http://127.0.0.1:{port}/v1",
                gateway_capability=token,
                timeout_seconds=timeout_seconds,
                protocol_version=protocol_version,
                scheduler_policy=scheduler_policy,
            ),
            ToolRegistry(definition if isinstance(definition, tuple) else (definition,)),
        ).execute(
            HarnessContext(
                scope.organization_id, scope.run_id, None, task, instructions=instructions
            ),
            trace,
            cancel_event,
        )
        return result, trace, accounting
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5)


def test_responses_final_json_excludes_intermediate_text_and_preserves_call_identity():
    from product_api.harness import CURRENT_TOOL_CALL
    from product_api.specialist_runner import validate_no_arguments

    class ExplanationProvider(ReplayResponsesProvider):
        async def stream(self, policy, payload):
            first = not any(i.get("type") == "function_call_output" for i in payload["input"])
            intro = {
                "type": "message",
                "id": "msg_intro",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "先查询证据。", "annotations": []}],
            }
            if first:
                yield {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**intro, "content": []},
                }
                yield {
                    "type": "response.output_text.delta",
                    "item_id": intro["id"],
                    "content_index": 0,
                    "delta": "先查询证据。",
                }
                yield {"type": "response.output_item.done", "output_index": 0, "item": intro}
            async for event in super().stream(policy, payload):
                if first and event["type"] == "response.completed":
                    event["response"]["output"].insert(0, intro)
                yield event

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
                "unknowns": ["fixture only"],
            }

        result, trace, accounting = await run_pi_fixture(
            ExplanationProvider(),
            ToolDefinition("commercial.quote_validity", evidence, validate_no_arguments),
            '{"role":"quote_validity"}',
        )
        assert result.output["role"] == "quote_validity"
        assert result.tool_calls == ("commercial.quote_validity",)
        assert "先查询" not in str(result.output)
        assert "|fc_" in captured[0]["toolCallId"]
        assert captured[0]["modelCallKey"] == accounting.calls[0]
        assert len(accounting.calls) == 2
        assert any(e["metadata"].get("modelCallKey") == accounting.calls[0] for e in trace.events)

    asyncio.run(scenario())


def test_responses_incomplete_fails_real_pi_even_when_usage_is_available():
    import pytest

    from product_api.harness import HarnessTurnError

    class IncompleteProvider:
        async def stream(self, policy, payload):
            yield {
                "type": "response.incomplete",
                "response": {
                    "id": "resp_incomplete",
                    "status": "incomplete",
                    "output": [],
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            }

    async def scenario():
        with pytest.raises(HarnessTurnError) as caught:
            await run_pi_fixture(
                IncompleteProvider(),
                ToolDefinition("supply.impact", lambda args: {}),
                "Investigate",
            )
        assert caught.value.code == "model_failed"

    asyncio.run(scenario())


def test_cached_usage_charges_are_exact_and_policy_changes_deny_before_reservation():
    from dataclasses import replace

    class CachedProvider:
        async def stream(self, policy, payload):
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_cached",
                    "status": "completed",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "input_tokens_details": {"cached_tokens": 8},
                    },
                },
            }

    async def scenario():
        gateway, accounting, _scope, token = _gateway()
        accounting.policy_value = replace(
            accounting.policy_value,
            input_price_micros_per_million=1_000_000,
            cached_input_price_micros_per_million=250_000,
            output_price_micros_per_million=2_000_000,
        )
        gateway.providers["replay"] = CachedProvider()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_model_gateway_app(gateway)),
            base_url="http://gateway",
        ) as client:
            payload = {"input": [{"role": "user", "content": "task"}], "stream": True}
            headers = {
                "authorization": f"Bearer {token}",
                "x-supplyagent-call-key": f"{uuid4()}-1",
                "x-supplyagent-policy-hash": "wrong",
            }
            assert (
                await client.post("/v1/responses", json=payload, headers=headers)
            ).status_code == 403
            assert accounting.calls == []
            headers["x-supplyagent-policy-hash"] = policy_hash(accounting.policy_value)
            assert (
                await client.post("/v1/responses", json=payload, headers=headers)
            ).status_code == 200
        assert accounting.settlements == [(10, 2, 8)]

    asyncio.run(scenario())
