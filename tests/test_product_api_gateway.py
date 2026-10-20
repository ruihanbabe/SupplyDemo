"""F34 HTTP capability, Replay stream and real Pi tool-loop boundary."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from product_api.gateway import (
    CapabilitySigner,
    GatewayError,
    ModelCapability,
    ModelGateway,
    ModelPolicy,
    OpenAIChatProvider,
    ReplayChatProvider,
    create_model_gateway_app,
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
        self.policy = policy
        self.calls: list[str] = []
        self.settlements: list[tuple[int, int, int]] = []

    async def reserve(self, scope: ModelCapability, call_key: str, input_digest: str):
        assert scope.policy_id == self.policy.id and len(input_digest) == 64
        self.calls.append(call_key)
        return self.policy, uuid4()

    async def settle(
        self, scope: ModelCapability, reservation_id, output_digest: str,
        input_tokens: int, output_tokens: int, cost_micros: int,
    ) -> None:
        assert scope.policy_id == self.policy.id and reservation_id and len(output_digest) == 64
        self.settlements.append((input_tokens, output_tokens, cost_micros))


def _gateway():
    policy = ModelPolicy(uuid4(), uuid4(), "replay", "fixed-v1", None, False, 1000, 600, 0, 0, 128)
    scope = ModelCapability(uuid4(), policy.organization_id, uuid4(), policy.id, int(time.time()) + 60)
    accounting = MemoryAccounting(policy)
    signer = CapabilitySigner(b"t" * 32)
    gateway = ModelGateway(signer, accounting, {"replay": ReplayChatProvider()})
    return gateway, accounting, scope, signer.issue(scope)


def test_http_capability_rejects_tampering_and_unregistered_tool() -> None:
    async def scenario() -> None:
        gateway, accounting, _scope, token = _gateway()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_model_gateway_app(gateway)), base_url="http://gateway")
        payload = {"model": "ignored", "messages": [{"role": "user", "content": "task"}], "stream": True}
        headers = {"authorization": f"Bearer {token}", "x-supplyagent-call-key": "call-1"}
        try:
            assert (await client.post("/v1/chat/completions", json=payload, headers={**headers, "authorization": f"Bearer {token}x"})).status_code == 403
            bad = {**payload, "tools": [{"type": "function", "function": {"name": "shell.exec"}}]}
            assert (await client.post("/v1/chat/completions", json=bad, headers=headers)).status_code == 403
            response = await client.post("/v1/chat/completions", json=payload, headers=headers)
            assert response.status_code == 200 and "data: [DONE]" in response.text
            assert accounting.calls == ["call-1"]
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
                    ("node", str(SIDECAR)), gateway_url=f"http://127.0.0.1:{port}/v1",
                    gateway_capability=token,
                ),
                ToolRegistry((ToolDefinition("supply.impact", lambda arguments: {"part": arguments["partNumber"]}),)),
            )
            result = await harness.execute(
                HarnessContext(scope.organization_id, scope.run_id, None, "Investigate PART-A"), trace,
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
                text='data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        policy = ModelPolicy(uuid4(), uuid4(), "openai", "server-model", "vault-ref-1", True, 1000, 600, 1, 2, 50)
        provider = OpenAIChatProvider(Resolver(), client)
        try:
            chunks = [chunk async for chunk in provider.stream(policy, {"model": "client-forged", "messages": [], "stream": True})]
            assert len(chunks) == 1 and chunks[0]["usage"]["prompt_tokens"] == 2
            assert len(observed) == 1
            assert observed[0].headers["authorization"] == "Bearer test-provider-key"
            assert observed[0].url.host == "api.openai.com"
            assert b'"model":"server-model"' in observed[0].content
            assert b'"max_completion_tokens":50' in observed[0].content
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_integer_cost_and_unknown_usage_preserve_budget_reservation() -> None:
    class ChargedProvider:
        async def stream(self, _policy, _payload):
            yield {"choices": [{"delta": {"content": "ok"}}]}
            yield {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    class InterruptedProvider:
        async def stream(self, _policy, _payload):
            yield {"choices": [{"delta": {"content": "partial"}}]}

    async def scenario() -> None:
        gateway, accounting, scope, _token = _gateway()
        accounting.policy = ModelPolicy(
            accounting.policy.id, scope.organization_id, "replay", "fixed-v1", None,
            False, 1000, 600, 500_000, 250_000, 128,
        )
        gateway.providers["replay"] = ChargedProvider()
        policy, reservation = await gateway.prepare(scope, "charged", {}, "a" * 64)
        assert [line async for line in gateway.stream(scope, policy, reservation, {})][-1] == b"data: [DONE]\n\n"
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
