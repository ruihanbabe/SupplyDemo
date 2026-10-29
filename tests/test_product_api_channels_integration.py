"""F37 real isolated SQL + encrypted independent SQLite, no network provider."""

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from psycopg import AsyncConnection
from psycopg import Error as DatabaseError
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api import channels
from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.channels import ChannelCipher, ChannelError, FixtureReceiver
from product_api.runtime import DurableWorker, PostgresRuntimeStore, WorkerIdentity, WorkerPolicy
from product_api.tenant import PostgresTenantRepository, TenantAccessError


@pytest.mark.integration
@pytest.mark.parametrize("fault", [None, "unapproved", "opt_out", "policy_changed", "expired", "requester_revoked", "payload_changed", "lost_response", "unknown", "known_rejection", "cancel_after_commit"])
def test_encrypted_send_gates_receipts_and_unknown_reconciliation(tmp_path, monkeypatch, fault):
    async def scenario():
        key = Fernet.generate_key()
        cipher = ChannelCipher(key)
        remote_path = tmp_path / "independent-recipient.sqlite"
        monkeypatch.setenv("SUPPLYAGENT_CHANNEL_MODE", "fixture")
        monkeypatch.setenv("SUPPLYAGENT_CHANNEL_ENCRYPTION_KEY", key.decode())
        monkeypatch.setenv("SUPPLYAGENT_CHANNEL_FIXTURE_DB", str(remote_path))
        instances = []
        original = channels.FixtureAdapter

        class FaultAdapter(original):
            def __init__(self, path, box):
                super().__init__(path, box, lose_response=fault == "lost_response" and not instances)
                instances.append(self)

            async def send(self, logical_id, payload):
                if fault == "known_rejection":
                    self.send_calls += 1
                    return channels.Receipt("failed", "fixture:rejected")
                if fault == "unknown":
                    self.send_calls += 1
                    raise TimeoutError("unknown before response")
                result = await super().send(logical_id, payload)
                if fault == "cancel_after_commit":
                    async with await AsyncConnection.connect(url) as db:
                        job_id = (await (await db.execute("select job_id from app.action_execution where logical_action_id=%s", (logical_id,))).fetchone())[0]
                    assert await store.cancel(job_id)
                return result

        monkeypatch.setattr(channels, "FixtureAdapter", FaultAdapter)
        async with tool_database("0031_f51_tool_resources.sql", "0043_f41_execution.sql", "0044_f37_channels.sql", once_migrations=("0023_f41_proposals.sql",)) as url:
            org, user, worker_id, _, store, _ = await seed_tool_job(url)
            # This scenario consumes only action jobs, never the helper's supply investigation.
            async with await AsyncConnection.connect(url) as db:
                seed_job_id = (await (await db.execute("select id from app.runtime_job where organization_id=%s", (org,))).fetchone())[0]
            assert await store.cancel(seed_job_id)
            approver, customer = uuid4(), uuid4()
            async with await AsyncConnection.connect(url) as db:
                await db.execute("insert into app.organization_member(organization_id,user_id,role) values(%s,%s,'approver')", (org, approver))
                await db.execute("insert into app.crm_customer(id,organization_id,display_name,created_by) values(%s,%s,'SYNTHETIC channel customer',%s)", (customer, org, user))
                tr = (await (await db.execute("insert into app.trace_run(organization_id,run_kind,actor_id) values(%s,'manual',%s) returning id", (org, user))).fetchone())[0]
                await db.execute("select app.set_request_context(%s,%s)", (user, org))
                evidence = (await (await db.execute("select app.append_trace_artifact(%s,null,%s,'manual_action_evidence','SYNTHETIC manual message evidence','{}')", (tr, uuid4()))).fetchone())[0]
                await db.execute("insert into app.action_service_policy(organization_id,revision,enabled) values(%s,1,true)", (org,))
            tenants = PostgresTenantRepository(url)

            class Auth:
                async def verify_access_token(self, token):
                    from uuid import UUID
                    return AuthenticatedIdentity(UUID(token), "synthetic@example.invalid")

            app = create_app(Auth(), tenants)
            headers = lambda actor: {"authorization": f"Bearer {actor}"}
            body = {"organizationId": str(org), "channel": "fixture", "targetId": str(customer),
                    "targetVersion": 1, "destination": "SYNTHETIC-recipient", "body": "SYNTHETIC-private-outbound",
                    "evidenceArtifactIds": [str(evidence)], "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "idempotencyKey": "message-one"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.post("/v1/message-proposals", json={**body, "channel": "whatsapp"}, headers=headers(user))).status_code == 422
                monkeypatch.delenv("SUPPLYAGENT_CHANNEL_ENCRYPTION_KEY")
                assert (await client.post("/v1/message-proposals", json=body, headers=headers(user))).status_code == 503
                monkeypatch.setenv("SUPPLYAGENT_CHANNEL_ENCRYPTION_KEY", key.decode())
                created = await client.post("/v1/message-proposals", json=body, headers=headers(user))
                assert created.status_code == 200, created.text
                proposal = created.json()["data"]["proposalId"]
                same = await client.post("/v1/message-proposals", json=body, headers=headers(user))
                assert same.status_code == 200 and same.json()["data"]["proposalId"] == proposal
                assert (await client.post("/v1/message-proposals", json={**body, "body": "changed"}, headers=headers(user))).status_code == 409
                detail = await client.get(f"/v1/proposals/{proposal}", params={"organization_id": str(org)}, headers=headers(approver))
                assert detail.status_code == 200 and detail.json()["data"]["content"]["body"] == body["body"]
                if fault != "unapproved":
                    reviewed = await client.post(f"/v1/proposals/{proposal}/review", json={"organizationId": str(org), "decision": "approved"}, headers=headers(approver))
                    assert reviewed.status_code == 200, reviewed.text
                    retried = await client.post("/v1/message-proposals", json=body, headers=headers(user))
                    assert retried.json()["data"]["status"] == "approved"
            async with await AsyncConnection.connect(url) as db:
                stored = (await (await db.execute("select content::text from app.action_proposal where id=%s", (proposal,))).fetchone())[0]
                assert body["body"] not in stored and body["destination"] not in stored
                if fault == "opt_out":
                    await db.execute("update app.crm_customer set action_opt_out=true where id=%s", (customer,))
                if fault == "policy_changed":
                    await db.execute("update app.action_service_policy set revision=2 where organization_id=%s", (org,))
                if fault == "expired":
                    await db.execute("update app.action_proposal set expires_at=clock_timestamp()-interval '1 second' where id=%s", (proposal,))
                if fault == "requester_revoked":
                    await db.execute("update app.organization_member set role='auditor' where user_id=%s and organization_id=%s", (user, org))
                if fault == "payload_changed":
                    await db.execute("update app.action_proposal set content=jsonb_set(content,'{payloadTag}',to_jsonb(repeat('0',64))) where id=%s", (proposal,))

            def no_model(job):
                raise AssertionError("send execution cannot invoke model")

            runtime = DurableWorker(store, store._identity, WorkerPolicy(30, 5, 3, 0), no_model)
            report = await runtime.run_once_report()
            if fault == "unapproved":
                assert report.outcome == "idle" and not instances and not remote_path.exists()
                return
            if fault in {"opt_out", "policy_changed", "expired", "requester_revoked", "payload_changed"}:
                assert report.outcome == "failed" and sum(a.send_calls for a in instances) == 0
                return
            if fault in {"lost_response", "unknown"}:
                assert report.outcome == "retry_scheduled", report
                # New Worker and Adapter objects: only the two persistent databases survive.
                identity = WorkerIdentity(org, worker_id, "restarted-channel-worker")
                store = PostgresRuntimeStore(tenants, identity)
                report = await DurableWorker(store, identity, WorkerPolicy(30, 5, 3, 0), no_model).run_once_report()
            assert sum(a.send_calls for a in instances) == 1
            if fault == "known_rejection":
                assert report.outcome == "failed"
                async with await AsyncConnection.connect(url) as db:
                    assert (await (await db.execute("select state from app.channel_send_ledger where proposal_id=%s", (proposal,))).fetchone())[0] == "failed"
                return
            if fault == "unknown":
                assert report.outcome == "retry_scheduled"
                final = await DurableWorker(store, store._identity, WorkerPolicy(30, 5, 3, 0), no_model).run_once_report()
                async with await AsyncConnection.connect(url) as db:
                    diagnostic = await (await db.execute("select j.status,j.attempts,j.visible_at,clock_timestamp() as server_now,j.last_error from app.runtime_job j join app.action_execution e on e.job_id=j.id where e.proposal_id=%s", (proposal,))).fetchone()
                assert final.outcome == "failed" and sum(a.send_calls for a in instances) == 1, (final, diagnostic)
                async with await AsyncConnection.connect(url) as db:
                    assert (await (await db.execute("select state from app.channel_send_ledger where proposal_id=%s", (proposal,))).fetchone())[0] == "reconciliation_required"
                return
            assert report.outcome == ("cancelled" if fault == "cancel_after_commit" else "completed"), report
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as reader:
                detail = await reader.get(f"/v1/proposals/{proposal}", params={"organization_id": str(org)}, headers=headers(approver))
                assert detail.status_code == 200 and detail.json()["data"]["send"]["state"] == ("sending" if fault == "cancel_after_commit" else "accepted")
            with sqlite3.connect(remote_path) as remote:
                assert remote.execute("select count(*) from delivery").fetchone()[0] == 1
            assert body["body"].encode() not in remote_path.read_bytes()
            async with await AsyncConnection.connect(url) as db:
                logical, state, reference = await (await db.execute("select logical_action_id,state,receipt from app.channel_send_ledger where proposal_id=%s", (proposal,))).fetchone()
                assert state == ("sending" if fault == "cancel_after_commit" else "accepted")
            if fault == "cancel_after_commit":
                reference = (await original(remote_path, cipher).lookup(str(logical))).reference
            signing_key = b"synthetic-independent-webhook-key-32-bytes"
            receiver = FixtureReceiver(tenants, store._identity, cipher, signing_key)
            event = {"logicalActionId": str(logical), "eventId": "fixture-event", "state": "delivered", "receipt": reference}
            raw = json.dumps(event).encode()
            timestamp = str(int(time.time()))
            signature = hmac.new(signing_key, timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
            with pytest.raises(ChannelError, match="signature"):
                await receiver.receive(raw, timestamp, "invalid")
            with pytest.raises(ChannelError, match="signature"):
                await receiver.receive(raw, str(int(timestamp)-3600), signature)
            assert await receiver.receive(raw, timestamp, signature) == "delivered"
            assert await receiver.receive(raw, timestamp, signature) == "delivered"
            altered = json.dumps({**event, "state": "failed"}).encode()
            altered_signature = hmac.new(signing_key, timestamp.encode() + b"." + altered, hashlib.sha256).hexdigest()
            with pytest.raises(DatabaseError) as conflict:
                await receiver.receive(altered, timestamp, altered_signature)
            assert conflict.value.sqlstate == "40901"
            foreign = FixtureReceiver(tenants, WorkerIdentity(uuid4(), worker_id, "foreign"), cipher, signing_key)
            with pytest.raises(TenantAccessError):
                await foreign.receive(raw, timestamp, signature)
            async with await AsyncConnection.connect(url) as db:
                assert (await (await db.execute("select count(*) from app.channel_inbox")).fetchone())[0] == 1
                assert (await (await db.execute("select state from app.channel_send_ledger where proposal_id=%s", (proposal,))).fetchone())[0] == "delivered"
                assert (await (await db.execute("select version from app.crm_customer where id=%s", (customer,))).fetchone())[0] == 1
                if fault == "cancel_after_commit":
                    assert (await (await db.execute("select j.status from app.runtime_job j join app.action_execution e on e.job_id=j.id where e.proposal_id=%s", (proposal,))).fetchone())[0] == "cancelled"
            late = json.dumps({**event, "eventId": "late-failure", "state": "failed"}).encode()
            late_signature = hmac.new(signing_key, timestamp.encode() + b"." + late, hashlib.sha256).hexdigest()
            assert await receiver.receive(late, timestamp, late_signature) == "delivered"
    asyncio.run(scenario())
