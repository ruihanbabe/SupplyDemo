"""F34 run-scoped HTTP model gateway. Provider secrets and SQL accounting stay in Python."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from psycopg import AsyncConnection
from psycopg import Error as PostgresError
from psycopg.types.json import Jsonb

from supplyagent.sdk.context.mapping import validate_projection
from supplyagent.service.gateway.glm import (
    COMPAT,
    GlmOpenAICompatibleProvider,
    effective_chat_request,
    validate_free_policy,
)
from supplyagent.service.gateway.model_tools import ALIASES
from supplyagent.service.persistence.context_store import ContextError
from supplyagent.service.persistence.tenant import (
    PostgresTenantRepository,
    TenantAccessError,
    TenantRepository,
)

_MAX_REQUEST_BYTES = 64 * 1024


class GatewayError(Exception):
    """A fail-closed request; only explicitly safe status metadata may be recorded."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


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
    reasoning: bool = False
    context_window: int = 32768
    cached_input_price_micros_per_million: int | None = None


@dataclass(frozen=True)
class ModelCapability:
    actor_id: UUID
    organization_id: UUID
    run_id: UUID
    policy_id: UUID
    expires_at: int
    job_id: UUID | None = None
    generation: int | None = None
    owner: str | None = None


class CapabilitySigner:
    def __init__(self, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("Gateway signing key must contain at least 32 bytes")
        self._key = signing_key

    def issue(self, capability: ModelCapability) -> str:
        binding = {}
        if any(v is not None for v in (capability.job_id, capability.generation, capability.owner)):
            if (
                capability.job_id is None or type(capability.generation) is not int
                or capability.generation < 1 or not isinstance(capability.owner, str)
                or not 1 <= len(capability.owner) <= 128
            ):
                raise ValueError("invalid job capability")
            binding = {"job": str(capability.job_id), "generation": capability.generation, "owner": capability.owner}
        body = json.dumps(
            {
                "actor": str(capability.actor_id),
                "organization": str(capability.organization_id),
                "run": str(capability.run_id),
                "policy": str(capability.policy_id),
                "expires": capability.expires_at,
                **binding,
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
            if len(encoded) > 1024 or len(signature) > 64:
                raise ValueError("oversized capability")
            expected = hmac.new(self._key, encoded.encode(), hashlib.sha256).digest()
            actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            if not hmac.compare_digest(expected, actual):
                raise ValueError("invalid signature")
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            base = {"actor", "organization", "run", "policy", "expires"}
            if set(payload) not in (base, base | {"job", "generation", "owner"}):
                raise ValueError("invalid scope")
            if "job" in payload and (
                type(payload["generation"]) is not int or payload["generation"] < 1
                or not isinstance(payload["owner"], str) or not 1 <= len(payload["owner"]) <= 128
            ):
                raise ValueError("invalid job binding")
            expiry = payload["expires"]
            if type(expiry) is not int or expiry <= time.time() or expiry > time.time() + 3600:
                raise ValueError("expired capability")
            return ModelCapability(
                UUID(payload["actor"]),
                UUID(payload["organization"]),
                UUID(payload["run"]),
                UUID(payload["policy"]),
                expiry,
                UUID(payload["job"]) if "job" in payload else None,
                payload.get("generation"), payload.get("owner"),
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise GatewayError("invalid model capability") from error


class ModelProvider(Protocol):
    async def stream(
        self, policy: ModelPolicy, payload: dict[str, object]
    ) -> AsyncIterator[dict[str, object]]: ...


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class EnvironmentCredentialResolver:
    """Deployment injects secrets into Python only under opaque policy references."""

    def resolve(self, reference: str) -> str:
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", reference):
            raise GatewayError("model credential unavailable")
        secret = os.getenv(f"SUPPLYAGENT_MODEL_SECRET_{reference}")
        if not secret and reference == "ZHIPU":
            secret = os.getenv("SUPPLYAGENT_LLM_ZHIPU_API_KEY")
        if not secret:
            raise GatewayError("model credential unavailable")
        return secret


class OpenAIResponsesProvider:
    """Only the Python process resolves BYOK; the sidecar sees a scoped Gateway capability."""

    def __init__(
        self, credentials: CredentialResolver, client: httpx.AsyncClient | None = None
    ) -> None:
        self._credentials = credentials
        self._client = client

    async def stream(
        self, policy: ModelPolicy, payload: dict[str, object]
    ) -> AsyncIterator[dict[str, object]]:
        if policy.credential_ref is None or not policy.real_calls_enabled:
            raise GatewayError("real model calls disabled")
        key = self._credentials.resolve(policy.credential_ref)
        if not key:
            raise GatewayError("model credential unavailable")
        request_body = {
            **payload,
            "model": policy.model_id,
            "max_output_tokens": policy.max_output_tokens,
            "store": False,
        }
        client = self._client or httpx.AsyncClient(timeout=60)
        try:
            async with client.stream(
                "POST",
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}"},
                json=request_body,
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


class ReplayResponsesProvider:
    """Native Responses fixtures exercise the real upstream converter, without a Provider."""

    async def stream(
        self, policy: ModelPolicy, payload: dict[str, object]
    ) -> AsyncIterator[dict[str, object]]:
        del policy
        inputs = payload["input"]
        results = [item for item in inputs if item.get("type") == "function_call_output"]
        tools = payload.get("tools", [])
        name = tools[0]["name"] if tools else None
        required = 2 if name == "supply_impact" else 1
        response_id = "resp_fixture_" + str(len(results))
        if name and len(results) < required:
            item = {
                "type": "function_call",
                "id": "fc_fixture_" + str(len(results)),
                "call_id": "call_fixture_" + str(len(results)),
                "name": name,
                "arguments": json.dumps(
                    {"partNumber": "PART-A"} if name == "supply_impact" else {}
                ),
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "arguments": ""},
            }
            midpoint = len(item["arguments"]) // 2
            for delta in (item["arguments"][:midpoint], item["arguments"][midpoint:]):
                yield {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "delta": delta,
                }
            yield {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "arguments": item["arguments"],
            }
            yield {"type": "response.output_item.done", "output_index": 0, "item": item}
            output = [item]
        else:
            text = "Replay completed after two Python supply impacts."
            task = next(
                (
                    part.get("text", "")
                    for item in inputs
                    if item.get("role") == "user"
                    for part in item.get("content", [])
                    if isinstance(part, dict) and part.get("type") == "input_text"
                ),
                "",
            )
            try:
                role = json.loads(task)["role"]
                evidence = json.loads(results[-1]["output"])
                citation = evidence["citation"]
                text = json.dumps(
                    {
                        "role": role,
                        "complete": True,
                        "claims": [
                            {
                                "subject": "opportunity",
                                "claimType": "business_answer",
                                "conclusion": "Responses 固定回放：已读取授权证据，待业务审校。",
                                "quality": "needs_review",
                                "citations": [citation],
                                "unknowns": evidence.get("unknowns", []),
                                "conflicts": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            except (ValueError, KeyError, IndexError, TypeError):
                pass
            item = {
                "type": "message",
                "id": "msg_fixture",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "content": []},
            }
            yield {
                "type": "response.output_text.delta",
                "item_id": item["id"],
                "content_index": 0,
                "delta": text,
            }
            yield {"type": "response.output_item.done", "output_index": 0, "item": item}
            output = [item]
        yield {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        }


class PostgresModelAccounting:
    def __init__(self, tenants: TenantRepository) -> None:
        self._tenants = tenants

    async def ready(self) -> None:
        async def inspect(connection):
            await connection.execute("set local statement_timeout = '3s'")
            for query in (
                "select id,organization_id,provider,model_id,credential_ref,real_calls_enabled,daily_budget_micros,max_call_cost_micros,input_price_micros_per_million,output_price_micros_per_million,max_output_tokens,reasoning,context_window,cached_input_price_micros_per_million,active from app.model_policy where false",
                "select id,organization_id,run_id,settled from app.model_reservation where false",
                "select run_id,input_tokens,output_tokens,cost_micros,outcome from app.model_usage where false",
                "select reservation_id,trace_run_id,actor_id,invocation_id,call_ordinal,schema_version,request,policy_snapshot,response,state,finished_at from app.model_exchange where false",
                "select id,organization_id,actor_id from app.trace_run where false",
            ):
                await connection.execute(query)
            for function in (
                "app.set_request_context(uuid,uuid)",
                "app.lock_responses_model_policy(uuid)",
                "app.reserve_model_budget(uuid,uuid,text,text)",
                "app.settle_model_budget(uuid,text,integer,integer,bigint,text)",
            ):
                row = await (
                    await connection.execute(
                        "select has_function_privilege(current_user,to_regprocedure(%s),'EXECUTE') as ok",
                        (function,),
                    )
                ).fetchone()
                if not row["ok"]:
                    raise GatewayError("model schema unavailable")
            row = await (
                await connection.execute(
                    "select has_table_privilege(current_user,'app.model_exchange','INSERT') and has_table_privilege(current_user,'app.model_exchange','UPDATE') as ok"
                )
            ).fetchone()
            if not row["ok"]:
                raise GatewayError("model schema unavailable")

        await self._tenants.in_readonly(inspect)

    async def _policy(self, connection, scope, *, lock=False):
        await self._guard_job(connection, scope)
        cursor = await connection.execute(
            "select * from app.lock_responses_model_policy(%s)"
            if lock
            else "select * from app.model_policy where id=%s and organization_id=%s and active",
            (scope.policy_id,) if lock else (scope.policy_id, scope.organization_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise GatewayError("model policy unavailable")
        fields = ModelPolicy.__dataclass_fields__
        return ModelPolicy(**{key: row[key] for key in fields if key in row})

    async def _guard_job(self, connection, scope):
        if scope.job_id is None:
            return  # Explicit legacy/standalone capabilities retain their original boundary.
        row = await (await connection.execute(
            "select app.lock_runtime_attempt(%s,%s,%s,%s) as valid",
            (scope.job_id, scope.run_id, scope.owner, scope.generation),
        )).fetchone()
        if not row["valid"]:
            raise GatewayError("model job capability revoked")

    async def policy(self, scope):
        return await self._tenants.in_organization(
            scope.actor_id, scope.organization_id, lambda c: self._policy(c, scope)
        )

    async def reserve(self, scope, call_key, input_digest):
        async def operation(connection):
            policy = await self._policy(connection, scope, lock=True)
            cursor = await connection.execute(
                "select app.reserve_model_budget(%s,%s,%s,%s) as id",
                (scope.policy_id, scope.run_id, call_key, input_digest),
            )
            return policy, (await cursor.fetchone())["id"]

        return await self._tenants.in_organization(scope.actor_id, scope.organization_id, operation)

    async def record(self, scope, reservation_id, call_key, payload, policy, *, context=None):
        invocation, ordinal = parse_call_key(call_key)

        async def operation(c):
            await self._guard_job(c, scope)
            schema = await (
                await c.execute("select to_regclass('app.context_snapshot') as present")
            ).fetchone()
            if context is None and schema["present"]:
                required = await (
                    await c.execute(
                        "select app.context_required(%s) as required",
                        (scope.run_id,),
                    )
                ).fetchone()
                if required["required"]:
                    raise GatewayError("context prepare required")
            request_hash = hashlib.sha256(
                json.dumps(
                    payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if context is not None:
                if not schema["present"]:
                    raise GatewayError("context schema unavailable")
                snapshot = await (
                    await c.execute(
                        "select projection from app.context_snapshot where id=%s",
                        (context["snapshotId"],),
                    )
                ).fetchone()
                if snapshot is None:
                    raise GatewayError("context snapshot unavailable")
                validate_projection(snapshot["projection"], payload, model_api(policy))
                await c.execute(
                    "select app.context_accept(%s,%s,%s,%s,%s,%s)",
                    (
                        context["snapshotId"],
                        scope.run_id,
                        call_key,
                        policy_hash(policy),
                        context["projectionHash"],
                        request_hash,
                    ),
                )
            columns = "reservation_id,organization_id,trace_run_id,actor_id,invocation_id,call_ordinal,request,policy_snapshot"
            values = (
                reservation_id,
                scope.organization_id,
                scope.run_id,
                scope.actor_id,
                invocation,
                ordinal,
                Jsonb(payload),
                Jsonb(public_policy(policy)),
            )
            if context is not None:
                columns += ",context_snapshot_id,projection_hash,request_hash"
                values += (context["snapshotId"], context["projectionHash"], request_hash)
            placeholders = ",".join(["%s"] * len(values))
            await c.execute(
                f"insert into app.model_exchange({columns}) values ({placeholders})", values
            )

        await self._tenants.in_organization(scope.actor_id, scope.organization_id, operation)

    async def finish(self, scope, reservation_id, response, state):
        async def operation(c):
            await c.execute(
                "update app.model_exchange set response=%s,state=%s,finished_at=clock_timestamp() where reservation_id=%s and state='running'",
                (Jsonb(response), state, reservation_id),
            )

        await self._tenants.in_organization(scope.actor_id, scope.organization_id, operation)

    async def settle(
        self,
        scope: ModelCapability,
        reservation_id: UUID,
        output_digest: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        outcome: str = "success",
        response: dict | None = None,
        state: str = "completed",
    ) -> None:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> None:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select app.settle_model_budget(%s,%s,%s,%s,%s,%s)",
                    (
                        reservation_id,
                        output_digest,
                        input_tokens,
                        output_tokens,
                        cost_micros,
                        outcome,
                    ),
                )
                if response is not None:
                    await cursor.execute(
                        "update app.model_exchange set response=%s,state=%s,finished_at=clock_timestamp() where reservation_id=%s and state='running' returning reservation_id",
                        (Jsonb(response), state, reservation_id),
                    )
                    if await cursor.fetchone() is None:
                        raise GatewayError("model exchange already finalized or absent")

        await self._tenants.in_organization(scope.actor_id, scope.organization_id, operation)


class ModelGateway:
    def __init__(
        self,
        signer: CapabilitySigner,
        accounting: PostgresModelAccounting,
        providers: dict[str, ModelProvider],
    ) -> None:
        self.signer = signer
        self.accounting = accounting
        self.providers = providers

    async def prepare(
        self,
        scope: ModelCapability,
        call_key: str,
        payload: dict[str, object],
        input_digest: str,
    ) -> tuple[ModelPolicy, UUID]:
        policy, reservation_id = await self.accounting.reserve(scope, call_key, input_digest)
        if reservation_id is None:
            raise GatewayError("daily model budget exhausted")
        return policy, reservation_id

    async def settle_native(self, scope, policy, reservation_id, digest, usage, final, state):
        if not isinstance(usage, dict):
            raise GatewayError("model usage unavailable")
        counts = [usage.get("input_tokens"), usage.get("output_tokens")]
        cached = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
        if any(type(x) is not int or x < 0 for x in [*counts, cached]) or cached > counts[0]:
            raise GatewayError("invalid model usage")
        cached_price = policy.cached_input_price_micros_per_million
        numerator = (
            (counts[0] - cached) * policy.input_price_micros_per_million
            + cached
            * (cached_price if cached_price is not None else policy.input_price_micros_per_million)
            + counts[1] * policy.output_price_micros_per_million
        )
        await self.accounting.settle(
            scope,
            reservation_id,
            digest,
            *counts,
            (numerator + 999999) // 1000000,
            outcome="success" if state == "completed" else "permanent_error",
            response=final,
            state=state,
        )

    async def stream_chat(self, scope, policy, reservation_id, payload):
        chunks, total_bytes, finish, usage = [], 0, None, None
        digest = hashlib.sha256()
        try:
            async for chunk in self.providers[policy.provider].stream(policy, payload):
                raw = json.dumps(chunk, separators=(",", ":")).encode()
                total_bytes += len(raw)
                if total_bytes > 900000:
                    raise GatewayError("private model exchange exceeds size limit")
                digest.update(raw)
                chunks.append(chunk)
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        raise GatewayError("multiple model choices are not supported")
                    if choice.get("finish_reason") is not None:
                        finish = choice["finish_reason"]
                yield b"data: " + raw + b"\n\n"
            state = "completed" if finish in {"stop", "tool_calls"} else "incomplete"
            normalized = (
                None
                if usage is None
                else {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "input_tokens_details": {
                        "cached_tokens": usage.get("prompt_tokens_details", {}).get(
                            "cached_tokens", 0
                        )
                    },
                }
            )
            final = {
                "protocol": "chat-completions",
                "chunks": chunks,
                "usage": usage,
                "finish_reason": finish,
            }
            await self.settle_native(
                scope, policy, reservation_id, digest.hexdigest(), normalized, final, state
            )
            if state != "completed":
                raise GatewayError("GLM did not complete")
            yield b"data: [DONE]\n\n"
        except BaseException as error:
            status = getattr(error, "status_code", None)
            await self.accounting.finish(
                scope,
                reservation_id,
                {
                    "protocol": "chat-completions",
                    "chunks": chunks,
                    "error": {
                        "code": "provider_authentication_failed"
                        if status in {401, 403}
                        else "model_stream_failed",
                        "httpStatus": status,
                    },
                },
                "failed" if status in {401, 403} else "interrupted",
            )
            raise

    async def stream(self, scope, policy, reservation_id, payload):
        provider = self.providers[policy.provider]
        output_hash = hashlib.sha256()
        final = None
        try:
            async for chunk in provider.stream(policy, payload):
                raw = json.dumps(chunk, separators=(",", ":")).encode()
                output_hash.update(raw)
                kind = chunk.get("type")
                if kind in {"response.completed", "response.incomplete", "response.failed"}:
                    final = chunk.get("response")
                    if not isinstance(final, dict):
                        raise GatewayError("invalid terminal response")
                    # Settle and persist before exposing a terminal success to Pi.
                    usage = final.get("usage")
                    if isinstance(usage, dict):
                        counts = [usage.get("input_tokens"), usage.get("output_tokens")]
                        cached = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
                        if (
                            any(type(x) is not int or x < 0 for x in [*counts, cached])
                            or cached > counts[0]
                        ):
                            raise GatewayError("invalid model usage")
                        cached_price = policy.cached_input_price_micros_per_million
                        numerator = (
                            (counts[0] - cached) * policy.input_price_micros_per_million
                            + cached
                            * (
                                cached_price
                                if cached_price is not None
                                else policy.input_price_micros_per_million
                            )
                            + counts[1] * policy.output_price_micros_per_million
                        )
                        await self.accounting.settle(
                            scope,
                            reservation_id,
                            output_hash.hexdigest(),
                            *counts,
                            (numerator + 999_999) // 1_000_000,
                            outcome="success"
                            if kind == "response.completed"
                            else "permanent_error",
                            response=final,
                            state=kind.removeprefix("response."),
                        )
                    else:
                        raise GatewayError("model usage unavailable")
                yield b"event: " + str(kind).encode() + b"\ndata: " + raw + b"\n\n"
                if kind in {"response.completed", "response.incomplete", "response.failed"}:
                    if kind != "response.completed":
                        yield b'event: error\ndata: {"type":"error","code":"model_incomplete","message":"model did not complete"}\n\n'
                    return
            if final is None:
                raise GatewayError("model stream interrupted")
        except BaseException:
            await self.accounting.finish(scope, reservation_id, final or {}, "interrupted")
            raise


def create_model_gateway_app(gateway: ModelGateway) -> FastAPI:
    """Mount only on a private interface; bearer capabilities are never browser sessions."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(5):
                await gateway.accounting.ready()
            return {"status": "ready"}
        except (OSError, TimeoutError, PostgresError, GatewayError, TenantAccessError):
            return JSONResponse({"status": "unavailable"}, status_code=503)

    def authorize(request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise GatewayError("invalid capability")
        return gateway.signer.verify(authorization[7:])

    @app.get("/v1/model")
    async def model(request: Request):
        try:
            scope = authorize(request)
            policy = await gateway.accounting.policy(scope)
            if policy.provider not in gateway.providers or (
                policy.provider != "replay" and not policy.real_calls_enabled
            ):
                raise GatewayError("model unavailable")
            if policy.provider == "zhipu":
                validate_free_policy(policy)
            return {
                "model": {
                    "id": policy.model_id,
                    "api": model_api(policy),
                    "provider": "zai" if policy.provider == "zhipu" else "openai",
                    **({"compat": COMPAT} if policy.provider == "zhipu" else {}),
                    "reasoning": policy.reasoning,
                    "input": ["text"],
                    "contextWindow": policy.context_window,
                    "maxTokens": policy.max_output_tokens,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                },
                "policyHash": policy_hash(policy),
            }
        except (GatewayError, PostgresError, TenantAccessError):
            return JSONResponse({"error": "model request denied"}, status_code=403)

    async def model_request(request: Request, api: str) -> Response:
        try:
            scope = authorize(request)
            call_key = request.headers.get("x-supplyagent-call-key", "")
            parse_call_key(call_key)
            raw = await request.body()
            if len(raw) > _MAX_REQUEST_BYTES:
                raise GatewayError("model request too large")
            payload = json.loads(raw)
            if api == "openai-responses":
                _validate_request(payload)
            else:
                validate_chat_request(payload)
            current = await gateway.accounting.policy(scope)
            if model_api(current) != api:
                raise GatewayError("model protocol mismatch")
            if current.provider == "zhipu":
                validate_free_policy(current)
            if request.headers.get("x-supplyagent-policy-hash") != policy_hash(current):
                raise GatewayError("model policy changed")
            if current.provider not in gateway.providers or (
                current.provider != "replay" and not current.real_calls_enabled
            ):
                raise GatewayError("model unavailable")
            policy, reservation_id = await gateway.prepare(
                scope, call_key, payload, hashlib.sha256(raw).hexdigest()
            )
            if policy_hash(current) != policy_hash(policy):
                raise GatewayError("model policy changed")
            # Persist exactly the effective request, not the client-forged model.
            payload = (
                effective_chat_request(policy, payload)
                if api == "openai-completions"
                else {
                    **payload,
                    "model": policy.model_id,
                    "max_output_tokens": policy.max_output_tokens,
                    "store": False,
                }
            )
            snapshot_id = request.headers.get("x-supplyagent-context-snapshot")
            projection_hash = request.headers.get("x-supplyagent-projection-hash")
            if snapshot_id is not None or projection_hash is not None:
                snapshot_id = UUID(snapshot_id or "")
                if not isinstance(projection_hash, str) or not re.fullmatch(
                    r"[a-f0-9]{64}", projection_hash
                ):
                    raise GatewayError("invalid context headers")
                await gateway.accounting.record(
                    scope,
                    reservation_id,
                    call_key,
                    payload,
                    policy,
                    context={"snapshotId": snapshot_id, "projectionHash": projection_hash},
                )
            else:
                await gateway.accounting.record(scope, reservation_id, call_key, payload, policy)
            return StreamingResponse(
                (gateway.stream_chat if api == "openai-completions" else gateway.stream)(
                    scope, policy, reservation_id, payload
                ),
                media_type="text/event-stream",
            )
        except (
            ContextError,
            GatewayError,
            ValueError,
            json.JSONDecodeError,
            PostgresError,
            TenantAccessError,
        ):
            return JSONResponse({"error": "model request denied"}, status_code=403)

    @app.post("/v1/responses")
    async def responses(request: Request):
        return await model_request(request, "openai-responses")

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        return await model_request(request, "openai-completions")

    return app


def create_default_model_gateway_app() -> FastAPI:
    """Private Gateway process; missing database/signing configuration fails startup."""
    required = (
        "SUPPLYAGENT_PG_USER",
        "SUPPLYAGENT_PG_PASSWORD",
        "SUPPLYAGENT_PG_HOST",
        "SUPPLYAGENT_PG_PORT",
        "SUPPLYAGENT_PG_DB",
        "SUPPLYAGENT_GATEWAY_SIGNING_KEY",
    )
    if any(not os.getenv(name) for name in required):
        raise RuntimeError("model gateway configuration missing")
    database_url = "postgresql://{user}:{password}@{host}:{port}/{database}".format(
        user=os.environ["SUPPLYAGENT_PG_USER"],
        password=os.environ["SUPPLYAGENT_PG_PASSWORD"],
        host=os.environ["SUPPLYAGENT_PG_HOST"],
        port=os.environ["SUPPLYAGENT_PG_PORT"],
        database=os.environ["SUPPLYAGENT_PG_DB"],
    )
    return create_model_gateway_app(
        ModelGateway(
            CapabilitySigner(os.environ["SUPPLYAGENT_GATEWAY_SIGNING_KEY"].encode()),
            PostgresModelAccounting(PostgresTenantRepository(database_url)),
            {
                "replay": ReplayResponsesProvider(),
                "openai": OpenAIResponsesProvider(EnvironmentCredentialResolver()),
                "zhipu": GlmOpenAICompatibleProvider(EnvironmentCredentialResolver()),
            },
        )
    )


def model_api(policy):
    return "openai-completions" if policy.provider == "zhipu" else "openai-responses"


def public_policy(policy):
    return json.loads(
        json.dumps(
            {
                **{k: v for k, v in asdict(policy).items() if k != "credential_ref"},
                "model_api": model_api(policy),
            },
            default=str,
        )
    )


def policy_hash(policy):
    return hashlib.sha256(
        json.dumps(public_policy(policy), sort_keys=True, default=str).encode()
    ).hexdigest()


def parse_call_key(key):
    try:
        invocation, ordinal = key.rsplit("-", 1)
        identity = UUID(invocation)
        number = int(ordinal)
        if not 1 <= number <= 1000 or key != f"{identity}-{number}":
            raise ValueError
        return identity, number
    except (ValueError, AttributeError):
        raise GatewayError("invalid model call key") from None


def _validate_request(payload):
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        raise GatewayError("streaming Responses request required")
    if set(payload) - {
        "model",
        "input",
        "stream",
        "tools",
        "tool_choice",
        "max_output_tokens",
        "temperature",
        "store",
        "reasoning",
        "include",
        "prompt_cache_key",
        "prompt_cache_retention",
        "service_tier",
        "text",
    }:
        raise GatewayError("unsupported model parameter")
    inputs = payload.get("input")
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 64:
        raise GatewayError("invalid Responses input")
    for item in inputs:
        if not isinstance(item, dict) or item.get("type", "message") not in {
            "message",
            "function_call",
            "function_call_output",
            "reasoning",
        }:
            raise GatewayError("unsupported Responses item")
        if item.get("type", "message") == "message" and item.get("role") not in {
            "system",
            "developer",
            "user",
            "assistant",
        }:
            raise GatewayError("invalid message role")
    tools = payload.get("tools", [])
    if (
        not isinstance(tools, list)
        or len(tools) > 16
        or any(
            not isinstance(t, dict)
            or t.get("type") != "function"
            or t.get("name") not in ALIASES.values()
            for t in tools
        )
    ):
        raise GatewayError("unregistered model tool")
    names = [t["name"] for t in tools]
    if len(names) != len(set(names)):
        raise GatewayError("duplicate tool")


def validate_chat_request(payload):
    if not isinstance(payload, dict) or payload.get("stream") is not True:
        raise GatewayError("streaming Chat request required")
    if set(payload) - {
        "model",
        "messages",
        "stream",
        "tools",
        "tool_choice",
        "max_tokens",
        "temperature",
        "thinking",
    }:
        raise GatewayError("unsupported GLM parameter")
    messages = payload.get("messages")
    if (
        not isinstance(messages, list)
        or not 1 <= len(messages) <= 64
        or any(
            not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant", "tool"}
            for m in messages
        )
    ):
        raise GatewayError("invalid GLM messages")
    tools = payload.get("tools", [])
    if (
        not isinstance(tools, list)
        or len(tools) > 16
        or any(
            not isinstance(t, dict)
            or t.get("type") != "function"
            or not isinstance(t.get("function"), dict)
            or t["function"].get("name") not in ALIASES.values()
            for t in tools
        )
    ):
        raise GatewayError("unregistered GLM tool")
    names = [t["function"]["name"] for t in tools]
    if len(names) != len(set(names)):
        raise GatewayError("duplicate GLM tool")
