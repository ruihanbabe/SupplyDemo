"""F37 encrypted fixture transport; accepted is never a delivered receipt."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID


class ChannelError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class ChannelCipher:
    def __init__(self, key):
        try:
            from cryptography.fernet import Fernet

            self._cipher = Fernet(key)
            self._tag_key = hmac.digest(key, b"supplyagent-channel-tag-v1", "sha256")
            self.key_id = hashlib.sha256(key).hexdigest()
        except (ImportError, TypeError, ValueError):
            raise ChannelError("channel_config_unavailable") from None

    def seal(self, value):
        return self._cipher.encrypt(canonical(value)).decode()

    def open(self, value):
        from cryptography.fernet import InvalidToken

        try:
            result = json.loads(self._cipher.decrypt(value.encode()))
            if not isinstance(result, dict):
                raise TypeError
            return result
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise ChannelError("channel_payload_invalid") from None

    def tag(self, value):
        return hmac.new(self._tag_key, canonical(value), hashlib.sha256).hexdigest()

    def proposal_content(self, value):
        return {"channel": "fixture", "sealedPayload": self.seal(value),
                "payloadTag": self.tag(value), "keyId": self.key_id}


def configured_cipher():
    if os.getenv("SUPPLYAGENT_CHANNEL_MODE") != "fixture":
        raise ChannelError("channel_config_unavailable")
    key = os.getenv("SUPPLYAGENT_CHANNEL_ENCRYPTION_KEY", "").encode()
    return ChannelCipher(key)


@dataclass(frozen=True)
class Receipt:
    state: str
    reference: str


class SendAdapter(Protocol):
    async def send(self, logical_id: str, payload: dict) -> Receipt: ...
    async def lookup(self, logical_id: str) -> Receipt | None: ...


class FixtureAdapter:
    """Independent persistent local recipient, encrypted at rest, no network calls."""

    def __init__(self, path, cipher, *, lose_response=False):
        self.path, self.cipher = Path(path), cipher
        self.lose_response = lose_response
        self.send_calls = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("create table if not exists delivery(id text primary key,tag text not null,payload text not null,receipt text not null)")

    def _send(self, logical_id, payload):
        tag = self.cipher.tag(payload)
        receipt = "fixture:" + str(UUID(logical_id))
        with sqlite3.connect(self.path) as db:
            db.execute("insert or ignore into delivery values(?,?,?,?)",
                       (logical_id, tag, self.cipher.seal(payload), receipt))
            prior = db.execute("select tag,receipt from delivery where id=?", (logical_id,)).fetchone()
            if prior[0] != tag:
                raise ChannelError("channel_idempotency_conflict")
        return Receipt("accepted", prior[1])

    async def send(self, logical_id, payload):
        self.send_calls += 1
        receipt = await asyncio.to_thread(self._send, logical_id, payload)
        if self.lose_response:
            raise TimeoutError("fixture committed but response was lost")
        return receipt

    async def lookup(self, logical_id):
        def read():
            with sqlite3.connect(self.path) as db:
                row = db.execute("select receipt from delivery where id=?", (logical_id,)).fetchone()
                return Receipt("accepted", row[0]) if row else None
        return await asyncio.to_thread(read)


class ChannelExecutor:
    def __init__(self, store, adapter: SendAdapter, cipher: ChannelCipher):
        self.store, self.adapter, self.cipher = store, adapter, cipher

    async def command(self, query, parameters):
        from psycopg import Error as DatabaseError

        from supplyagent.service.persistence.tenant import TenantAccessError

        async def operation(c):
            return (await (await c.execute(query, parameters)).fetchone())["result"]
        try:
            return await self.store._tenants.in_organization(
                self.store._identity.actor_id, self.store._identity.organization_id, operation
            )
        except (DatabaseError, TenantAccessError) as error:
            cause = error.__cause__ or error
            if getattr(cause, "sqlstate", None) == "42501" and str(cause).startswith("lease_lost"):
                raise ChannelError("lease_lost") from None
            raise

    async def run(self, job, cancel=None):
        parameters = (job.proposal_id, job.id, self.store._identity.owner, job.generation)
        prepared = await self.command("select app.prepare_channel_send(%s,%s,%s,%s) as result", parameters)
        if prepared["state"] in {"accepted", "delivered"}:
            return "succeeded"
        if prepared["state"] in {"rejected", "failed"}:
            return "rejected"
        if cancel is not None and cancel.is_set():
            return "cancelled"
        if prepared["maySend"]:
            content = prepared["payload"]
            payload = self.cipher.open(content["sealedPayload"])
            if (
                content.get("channel") != "fixture" or content.get("keyId") != self.cipher.key_id
                or content.get("payloadTag") != self.cipher.tag(payload)
                or payload.get("organizationId") != str(self.store._identity.organization_id)
                or payload.get("targetId") != prepared["targetId"]
                or payload.get("targetVersion") != prepared["targetVersion"]
                or payload.get("actorId") != prepared["requestedBy"]
            ):
                raise ChannelError("channel_payload_invalid")
            allowed = await self.command("select app.action_preflight(%s,%s,%s,%s) as result", parameters)
            if not allowed:
                return await self.finish(parameters, "failed", None)
            try:
                receipt = await self.adapter.send(prepared["logicalActionId"], payload)
            except (TimeoutError, ConnectionError):
                await self.finish(parameters, "reconciliation_required", None)
                return "reconciliation_required"
        else:
            try:
                receipt = await self.adapter.lookup(prepared["logicalActionId"])
            except (TimeoutError, ConnectionError):
                receipt = None
            if receipt is None:
                await self.finish(parameters, "reconciliation_required", None)
                return "reconciliation_required"
        if receipt.state not in {"accepted", "failed"} or not receipt.reference:
            raise ChannelError("channel_payload_invalid")
        return await self.finish(parameters, receipt.state, receipt.reference)

    async def finish(self, parameters, state, reference):
        result = await self.command("select app.finish_channel_send(%s,%s,%s,%s,%s,%s) as result",
                                    (*parameters, state, reference))
        return "succeeded" if result in {"accepted", "delivered"} else "rejected" if result in {"failed", "rejected"} else result


class FixtureReceiver:
    """HMAC fixture protocol only; not a WhatsApp or Feishu signature implementation."""

    def __init__(self, tenants, identity, cipher, signing_key):
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ChannelError("channel_config_unavailable")
        self.tenants, self.identity, self.cipher, self.signing_key = tenants, identity, cipher, signing_key

    async def receive(self, raw, timestamp, signature):
        try:
            if not isinstance(raw, bytes) or len(raw) > 16384 or abs(time.time() - int(timestamp)) > 300:
                raise ValueError
            expected = hmac.new(self.signing_key, str(timestamp).encode() + b"." + raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise ValueError
            body = json.loads(raw)
            if set(body) != {"logicalActionId", "eventId", "state", "receipt"} or body["state"] not in {"delivered", "failed"}:
                raise ValueError
            logical_id = UUID(body["logicalActionId"])
            if any(not isinstance(body[k], str) or not 1 <= len(body[k]) <= 160 for k in ("eventId", "receipt")):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ChannelError("channel_signature_invalid") from None

        async def operation(c):
            return (await (await c.execute(
                "select app.record_channel_receipt(%s,%s,%s,%s,%s,%s) as state",
                (logical_id, body["eventId"], body["state"], body["receipt"], self.cipher.tag(body), self.cipher.seal(body)),
            )).fetchone())["state"]
        return await self.tenants.in_organization(self.identity.actor_id, self.identity.organization_id, operation)
