"""F34 run-scoped HTTP model gateway. Provider secrets and SQL accounting stay in Python."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from psycopg import AsyncConnection
from psycopg import Error as PostgresError

from product_api.tenant import PostgresTenantRepository, TenantRepository

_CALL_KEY = re.compile(r"^[a-zA-Z0-9_-]{1,120}$")
_MAX_REQUEST_BYTES = 64 * 1024


class GatewayError(Exception):
    """A fail-closed model request with no provider details in the response."""


@dataclass(frozen=True)
class ModelPolicy:
    id: UUID
    organization_id: UUID
    provider: str
    model_id: str
    credential_ref: str | None
    real_calls_enabled: bool
    daily_budget_micros: int
    max_call_cost_micros: int
    input_price_micros_per_million: int
    output_price_micros_per_million: int
    max_output_tokens: int


@dataclass(frozen=True)
class ModelCapability:
    actor_id: UUID
    organization_id: UUID
    run_id: UUID
    policy_id: UUID
    expires_at: int


class CapabilitySigner:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("Gateway signing key must contain at least 32 bytes")
        self._key = signing_key

    def issue(self, capability: ModelCapability) -> str:
        body = json.dumps(
            {
                "actor": str(capability.actor_id),
                "organization": str(capability.organization_id),
                "run": str(capability.run_id),
                "policy": str(capability.policy_id),
                "expires": capability.expires_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(body).rstrip(b"=")
        signature = hmac.new(self._key, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def verify(self, token: str) -> ModelCapability:
        try:
            encoded, signature = token.split(".", 1)
            if len(encoded) > 512 or len(signature) > 64:
                raise ValueError("oversized capability")
            expected = hmac.new(self._key, encoded.encode(), hashlib.sha256).digest()
            actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            if not hmac.compare_digest(expected, actual):
                raise ValueError("invalid signature")
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if set(payload) != {"actor", "organization", "run", "policy", "expires"}:
                raise ValueError("invalid scope")
            expiry = payload["expires"]
            if type(expiry) is not int or expiry <= time.time() or expiry > time.time() + 3600:
                raise ValueError("expired capability")
            return ModelCapability(
                UUID(payload["actor"]), UUID(payload["organization"]),
                UUID(payload["run"]), UUID(payload["policy"]), expiry,
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise GatewayError("invalid model capability") from error


class ModelProvider(Protocol):
    async def stream(self, policy: ModelPolicy, payload: dict[str, object]) -> AsyncIterator[dict[str, object]]: ...


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class EnvironmentCredentialResolver:
    """Deployment injects secrets into Python only under opaque policy references."""

    def resolve(self, reference: str) -> str:
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", reference):
            raise GatewayError("model credential unavailable")
        secret = os.getenv(f"SUPPLYAGENT_MODEL_SECRET_{reference}")
        if not secret:
            raise GatewayError("model credential unavailable")
        return secret


class OpenAIChatProvider:
    """Only the Python process resolves BYOK; the sidecar sees a scoped Gateway capability."""

    def __init__(self, credentials: CredentialResolver, client: httpx.AsyncClient | None = None) -> None:
        self._credentials = credentials
        self._client = client

    async def stream(self, policy: ModelPolicy, payload: dict[str, object]) -> AsyncIterator[dict[str, object]]:
        if policy.credential_ref is None or not policy.real_calls_enabled:
            raise GatewayError("real model calls disabled")
        key = self._credentials.resolve(policy.credential_ref)
        if not key:
            raise GatewayError("model credential unavailable")
        request_body = {**payload, "model": policy.model_id, "max_completion_tokens": policy.max_output_tokens}
        request_body.pop("max_tokens", None)
        client = self._client or httpx.AsyncClient(timeout=60)
        try:
            async with client.stream(
                "POST", "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"}, json=request_body,
            ) as response:
                if response.status_code != 200:
                    raise GatewayError("model provider request failed")
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError as error:
                        raise GatewayError("invalid provider stream") from error
                    if not isinstance(chunk, dict):
                        raise GatewayError("invalid provider stream")
                    yield chunk
        finally:
            if self._client is None:
                await client.aclose()


class ReplayChatProvider:
    """Deterministic HTTP fixture; never reaches a paid Provider."""

    async def stream(self, policy: ModelPolicy, payload: dict[str, object]) -> AsyncIterator[dict[str, object]]:
        del policy
        tool_results = sum(message.get("role") == "tool" for message in payload["messages"])
        if tool_results < 2:
            delta: dict[str, object] = {
                "tool_calls": [{
                    "index": 0, "id": f"impact-{tool_results + 1}", "type": "function",
                    "function": {"name": "supply.impact", "arguments": '{"partNumber":"PART-A"}'},
                }]
            }
            finish = "tool_calls"
        else:
            delta = {"content": "Replay completed after two Python supply impacts."}
            finish = "stop"
        yield {"id": "replay", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        yield {"id": "replay", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
        yield {"id": "replay", "object": "chat.completion.chunk", "choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


class PostgresModelAccounting:
    def __init__(self, tenants: TenantRepository) -> None:
        self._tenants = tenants

    async def reserve(
        self, scope: ModelCapability, call_key: str, input_digest: str
    ) -> tuple[ModelPolicy, UUID | None]:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> tuple[ModelPolicy, UUID | None]:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select * from app.model_policy where id=%s and organization_id=%s and active",
                    (scope.policy_id, scope.organization_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise GatewayError("model policy unavailable")
                policy = ModelPolicy(
                    id=row["id"], organization_id=row["organization_id"],
                    provider=row["provider"], model_id=row["model_id"],
                    credential_ref=row["credential_ref"], real_calls_enabled=row["real_calls_enabled"],
                    daily_budget_micros=row["daily_budget_micros"],
                    max_call_cost_micros=row["max_call_cost_micros"],
                    input_price_micros_per_million=row["input_price_micros_per_million"],
                    output_price_micros_per_million=row["output_price_micros_per_million"],
                    max_output_tokens=row["max_output_tokens"],
                )
                await cursor.execute(
                    "select app.reserve_model_budget(%s,%s,%s,%s) as id",
                    (scope.policy_id, scope.run_id, call_key, input_digest),
                )
                result = await cursor.fetchone()
                assert result is not None
                return policy, result["id"]

        return await self._tenants.in_organization(scope.actor_id, scope.organization_id, operation)

    async def settle(
        self, scope: ModelCapability, reservation_id: UUID, output_digest: str,
        input_tokens: int, output_tokens: int, cost_micros: int,
    ) -> None:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> None:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select app.settle_model_budget(%s,%s,%s,%s,%s,'success')",
                    (reservation_id, output_digest, input_tokens, output_tokens, cost_micros),
                )

        await self._tenants.in_organization(scope.actor_id, scope.organization_id, operation)


class ModelGateway:
    def __init__(
        self, signer: CapabilitySigner, accounting: PostgresModelAccounting,
        providers: dict[str, ModelProvider],
    ) -> None:
        self.signer = signer
        self.accounting = accounting
        self.providers = providers

    async def prepare(
        self, scope: ModelCapability, call_key: str, payload: dict[str, object],
        input_digest: str,
    ) -> tuple[ModelPolicy, UUID]:
        policy, reservation_id = await self.accounting.reserve(scope, call_key, input_digest)
        if reservation_id is None:
            raise GatewayError("daily model budget exhausted")
        return policy, reservation_id

    async def stream(
        self, scope: ModelCapability, policy: ModelPolicy, reservation_id: UUID,
        payload: dict[str, object],
    ) -> AsyncIterator[bytes]:
        provider = self.providers[policy.provider]
        output_hash = hashlib.sha256()
        usage: tuple[int, int] | None = None
        async for chunk in provider.stream(policy, payload):
            raw = json.dumps(chunk, separators=(",", ":")).encode()
            output_hash.update(raw)
            observed = chunk.get("usage")
            if isinstance(observed, dict):
                prompt, completion = observed.get("prompt_tokens"), observed.get("completion_tokens")
                if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
                    usage = prompt, completion
            yield b"data: " + raw + b"\n\n"
        if usage is None:
            # A dropped stream may still be billed. Retain the reservation for reconciliation.
            raise GatewayError("model usage unavailable")
        charge_numerator = (
            usage[0] * policy.input_price_micros_per_million
            + usage[1] * policy.output_price_micros_per_million
        )
        cost = (charge_numerator + 999_999) // 1_000_000
        await self.accounting.settle(scope, reservation_id, output_hash.hexdigest(), *usage, cost)
        yield b"data: [DONE]\n\n"


def create_model_gateway_app(gateway: ModelGateway) -> FastAPI:
    """Mount only on a private interface; bearer capabilities are never browser sessions."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completion(request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"error": "invalid model capability"}, status_code=401)
        try:
            scope = gateway.signer.verify(authorization[7:])
            call_key = request.headers.get("x-supplyagent-call-key", "")
            if not _CALL_KEY.fullmatch(call_key):
                raise GatewayError("invalid model call key")
            raw = await request.body()
            if len(raw) > _MAX_REQUEST_BYTES:
                raise GatewayError("model request too large")
            payload = json.loads(raw)
            _validate_request(payload)
            input_digest = hashlib.sha256(raw).hexdigest()
            policy, reservation_id = await gateway.prepare(scope, call_key, payload, input_digest)
            if policy.provider not in gateway.providers:
                raise GatewayError("model provider unavailable")
            if policy.provider != "replay" and not policy.real_calls_enabled:
                raise GatewayError("real model calls disabled")
            stream = gateway.stream(scope, policy, reservation_id, payload)
            return StreamingResponse(stream, media_type="text/event-stream")
        except (GatewayError, ValueError, json.JSONDecodeError, PostgresError):
            return JSONResponse({"error": "model request denied"}, status_code=403)

    return app


def create_default_model_gateway_app() -> FastAPI:
    """Private Gateway process; missing database/signing configuration fails startup."""
    required = (
        "SUPPLYAGENT_PG_USER", "SUPPLYAGENT_PG_PASSWORD", "SUPPLYAGENT_PG_HOST",
        "SUPPLYAGENT_PG_PORT", "SUPPLYAGENT_PG_DB", "SUPPLYAGENT_GATEWAY_SIGNING_KEY",
    )
    if any(not os.getenv(name) for name in required):
        raise RuntimeError("model gateway configuration missing")
    database_url = "postgresql://{user}:{password}@{host}:{port}/{database}".format(
        user=os.environ["SUPPLYAGENT_PG_USER"], password=os.environ["SUPPLYAGENT_PG_PASSWORD"],
        host=os.environ["SUPPLYAGENT_PG_HOST"], port=os.environ["SUPPLYAGENT_PG_PORT"],
        database=os.environ["SUPPLYAGENT_PG_DB"],
    )
    return create_model_gateway_app(ModelGateway(
        CapabilitySigner(os.environ["SUPPLYAGENT_GATEWAY_SIGNING_KEY"].encode()),
        PostgresModelAccounting(PostgresTenantRepository(database_url)),
        {"replay": ReplayChatProvider(), "openai": OpenAIChatProvider(EnvironmentCredentialResolver())},
    ))


def _validate_request(payload: object) -> None:
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        raise GatewayError("streaming model request required")
    if set(payload) - {"model", "messages", "stream", "stream_options", "tools", "tool_choice", "max_tokens", "max_completion_tokens", "temperature", "store"}:
        raise GatewayError("unsupported model parameter")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise GatewayError("invalid model messages")
    if any(not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"} for message in messages):
        raise GatewayError("invalid model role")
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 1 or any(
        not isinstance(tool, dict) or tool.get("type") != "function"
        or not isinstance(tool.get("function"), dict)
        or tool["function"].get("name") != "supply.impact" for tool in tools
    ):
        raise GatewayError("unregistered model tool")
