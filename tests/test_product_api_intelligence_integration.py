"""F45 HTTP -> SQL queue -> real Pi -> private snapshots; local services only."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.harness import HarnessTurnError, PiRuntimeAdapter
from product_api.knowledge import (
    DocumentKind,
    DocumentVisibility,
    KnowledgeChunkInput,
    PostgresKnowledgeRepository,
    SourceKind,
)
from product_api.runtime import DurableWorker, PostgresRuntimeStore, WorkerIdentity, WorkerPolicy
from product_api.tenant import PostgresTenantRepository, TenantAccessError
from product_api.tool_registry import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_intelligence_question_is_private_durable_and_scoped():
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local PostgreSQL is not configured")
    asyncio.run(scenario(source))


async def scenario(source):
    name = "f45_test_" + uuid4().hex
    admin_url = make_conninfo(source, dbname="postgres")
    clone = make_conninfo(source, dbname=name)
    async with await AsyncConnection.connect(admin_url, autocommit=True) as admin:
        await admin.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(conninfo_to_dict(source)["dbname"])
            )
        )
        try:
            await run_in_clone(clone)
        finally:
            await admin.execute(
                sql.SQL("drop database {} with (force)").format(sql.Identifier(name))
            )


async def run_in_clone(url):
    org, actor, peer, worker, other_org = (uuid4() for _ in range(5))
    async with await AsyncConnection.connect(url) as db:
        migration = (ROOT / "supabase/migrations/0024_f45_intelligence.sql").read_text()
        await db.execute(migration)
        await db.execute(migration)  # Make target is safe after this migration is already applied.
        await db.execute((ROOT / "supabase/migrations/0030_f51_tool_execution.sql").read_text())
        await db.execute((ROOT / "supabase/migrations/0042_runtime_attempt_guard.sql").read_text())
        await db.execute((ROOT / "supabase/migrations/0031_f51_tool_resources.sql").read_text())
        await db.execute(
            "insert into app.organization(id,slug,display_name) values (%s,%s,'F45'),(%s,%s,'Other')",
            (org, "f45-" + org.hex, other_org, "f45-" + other_org.hex),
        )
        for member in (actor, peer):
            await db.execute(
                "insert into app.organization_member(organization_id,user_id,role) values (%s,%s,'organization_admin')",
                (org, member),
            )
        await db.execute(
            "insert into app.organization_member(organization_id,user_id,role,scope) values (%s,%s,'maintainer','{\"runtimeWorker\":true}')",
            (org, worker),
        )
        cursor = await db.execute(
            "insert into app.crm_customer(organization_id,display_name,lifecycle,created_by) values (%s,'SYNTHETIC Customer','active',%s) returning id",
            (org, actor),
        )
        customer = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "insert into app.crm_opportunity(organization_id,customer_id,title,stage,created_by) values (%s,%s,'SYNTHETIC Opportunity','negotiation',%s) returning id",
            (org, customer, actor),
        )
        opportunity = (await cursor.fetchone())[0]
        await db.commit()

    class Auth:
        async def verify_access_token(self, token):
            return AuthenticatedIdentity(
                peer if token == "peer" else actor, "synthetic@example.invalid"
            )

    tenants = PostgresTenantRepository(url)
    app = create_app(auth_verifier=Auth(), tenant_repository=tenants)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"authorization": "Bearer actor"},
    ) as client:
        payload = {
            "organization_id": str(org),
            "opportunity_id": str(opportunity),
            "question": "这个商机缺少哪些跟进证据？",
            "idempotency_key": "f45-question",
        }
        response = await client.post("/v1/intelligence/runs", json=payload)
        assert response.status_code == 202, response.text
        run_id = response.json()["data"]["runId"]
        repeated = await client.post("/v1/intelligence/runs", json=payload)
        assert repeated.json() == response.json()
        changed = await client.post(
            "/v1/intelligence/runs", json={**payload, "question": "另一个问题"}
        )
        assert changed.status_code == 409
        path = f"/v1/intelligence/runs/{run_id}"
        assert (
            await client.get(
                path, params={"organization_id": str(org)}, headers={"authorization": "Bearer peer"}
            )
        ).status_code == 403
        assert (
            await client.get(path, params={"organization_id": str(other_org)})
        ).status_code == 403
        identity = WorkerIdentity(org, worker, "f45-test-worker")
        store = PostgresRuntimeStore(tenants, identity)
        runtime = DurableWorker(
            store,
            identity,
            WorkerPolicy(30, 5, 1, 0),
            lambda job: PiRuntimeAdapter(
                ("node", str(ROOT / "pi_sidecar/f42_protocol_sidecar.mjs")), timeout_seconds=20, protocol_version=2
            ),
        )
        assert await runtime.run_once()
        answer = await client.get(path, params={"organization_id": str(org)})
        assert answer.status_code == 200, answer.text
        data = answer.json()["data"]
        assert data["status"] == "completed", data
        assert data["question"] == payload["question"]
        assert data["mode"] == "replay" and data["quality"] == "needs_review"
        assert len(data["steps"]) == 3
        assert all(s["status"] == "completed" for s in data["steps"])
        assert (
            data["result"]["claims"][0]["citations"][0]["sourceKind"] == "intelligence_observation"
        )
        assert payload["question"] in data["result"]["claims"][0]["conclusion"]
        peer_reuse = await client.post(
            "/v1/intelligence/runs", json=payload, headers={"authorization": "Bearer peer"}
        )
        assert peer_reuse.status_code == 409
        # A user's private opportunity remains investigable without granting Worker CRM access.
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.crm_opportunity set scope=%s where id=%s",
                ('{"member_ids":["' + str(actor) + '"]}', opportunity),
            )
            await db.commit()
        retry_payload = {**payload, "idempotency_key": "f45-retry"}
        retry_response = await client.post("/v1/intelligence/runs", json=retry_payload)
        retry_run = retry_response.json()["data"]["runId"]

        class BrokenEngine:
            async def run_turn(self, *args, **kwargs):
                raise HarnessTurnError("model_failed")

        failing = DurableWorker(
            store, identity, WorkerPolicy(30, 5, 2, 0), lambda job: BrokenEngine()
        )
        claimed = await failing.run_once()
        if not claimed:
            async with await AsyncConnection.connect(url) as diagnostic:
                cursor = await diagnostic.execute(
                    "select j.kind,j.status,j.last_error,j.visible_at,clock_timestamp(),m.context_ref from app.runtime_job j join app.runtime_run r on r.id=j.run_id join app.runtime_mission m on m.id=r.mission_id where r.id=%s",
                    (retry_run,),
                )
                state = await cursor.fetchall()
            raise AssertionError(state)

        first_attempt = (
            await client.get(
                f"/v1/intelligence/runs/{retry_run}", params={"organization_id": str(org)}
            )
        ).json()["data"]
        assert first_attempt["status"] == "queued"
        assert len(first_attempt["steps"]) == 2 and all(
            s["status"] == "failed" for s in first_attempt["steps"]
        )
        assert await runtime.run_once()
        resumed = (
            await client.get(
                f"/v1/intelligence/runs/{retry_run}", params={"organization_id": str(org)}
            )
        ).json()["data"]
        assert resumed["status"] == "completed" and resumed["generation"] == 2
        assert len(resumed["steps"]) == 5 and resumed["result"]
        assert {s["generation"] for s in resumed["steps"]} == {1, 2}

        fenced = await client.post(
            "/v1/intelligence/runs", json={**payload, "idempotency_key": "f45-fence"}
        )
        job = await store.claim(30)
        invocation = uuid4()

        async def attempt_write(connection, status):
            await connection.execute(
                "select app.intelligence_attempt_write(%s,%s,%s,%s,'customer_facts','{}',%s,null,null)",
                (invocation, job.id, identity.owner, job.generation, status),
            )

        await tenants.in_organization(worker, org, lambda c: attempt_write(c, "running"))
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.runtime_job set lease_expires_at=clock_timestamp()-interval '1 second' where id=%s",
                (job.id,),
            )
            await db.commit()
        with pytest.raises(TenantAccessError):
            await tenants.in_organization(worker, org, lambda c: attempt_write(c, "completed"))
        assert await runtime.run_once()
        recovered = (
            await client.get(
                "/v1/intelligence/runs/" + fenced.json()["data"]["runId"],
                params={"organization_id": str(org)},
            )
        ).json()["data"]
        assert recovered["status"] == "completed" and recovered["generation"] == 2
        assert any(s["error_code"] == "lease_superseded" for s in recovered["steps"])
        knowledge = PostgresKnowledgeRepository()

        async def publish_policy(c):
            document = await knowledge.create_document(
                c,
                "SYNTHETIC approval policy",
                DocumentKind.BUSINESS_POLICY,
                DocumentVisibility.RESTRICTED,
                SourceKind.SYNTHETIC,
                None,
            )
            await knowledge.grant_role(c, document, "organization_admin")
            version = await knowledge.publish_version(
                c,
                document,
                "f45-v1",
                "synthetic:f45",
                "Approval requires human review.",
                (KnowledgeChunkInput(0, "section:approval", "Approval requires human review."),),
            )
            return version

        version = await tenants.in_organization(actor, org, publish_policy)
        policy_run = await client.post(
            "/v1/intelligence/runs",
            json={**payload, "question": "approval", "idempotency_key": "f45-knowledge"},
        )
        assert await runtime.run_once()
        policy_path = "/v1/intelligence/runs/" + policy_run.json()["data"]["runId"]
        policy_answer = (
            await client.get(policy_path, params={"organization_id": str(org)})
        ).json()["data"]
        assert any(
            "Approval requires human review." in e["excerpt"] for e in policy_answer["evidence"]
        )
        await tenants.in_organization(
            actor, org, lambda c: knowledge.revoke_version(c, version, "Synthetic withdrawal")
        )
        withdrawn = (await client.get(policy_path, params={"organization_id": str(org)})).json()[
            "data"
        ]
        assert withdrawn["result"] is None and not withdrawn["evidence"]
        assert withdrawn["issues"] == ["source_permission_or_version_changed"]

        revoked = await client.post(
            "/v1/intelligence/runs", json={**payload, "idempotency_key": "f45-role-revoked"}
        )
        revoked_id = revoked.json()["data"]["runId"]
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.organization_member set role='buyer' where organization_id=%s and user_id=%s",
                (org, actor),
            )
            await db.commit()
        assert (await tenants.select_organization(actor, org)).role == "buyer"
        assert (await tenants.select_organization(peer, org)).role == "organization_admin"
        assert await runtime.run_once()
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select status,last_error from app.runtime_job where run_id=%s", (revoked_id,)
            )
            assert await cursor.fetchone() == ("failed", "tool_failed")
            cursor = await db.execute(
                "select count(*) from app.intelligence_attempt where mission_id=%s",
                (revoked.json()["data"]["missionId"],),
            )
            assert (await cursor.fetchone())[0] == 0
            await db.execute(
                "update app.organization_member set role='organization_admin' where organization_id=%s and user_id=%s",
                (org, actor),
            )
            await db.commit()

        async def private_counts(c):
            cursor = await c.execute("select count(*) as count from app.intelligence_observation")
            return (await cursor.fetchone())["count"]

        assert await tenants.in_organization(peer, org, private_counts) == 0
        async with await AsyncConnection.connect(url) as db:
            cur = await db.execute(
                "select input_context from app.intelligence_attempt where mission_id=%s and role=%s",
                (response.json()["data"]["missionId"], "opportunity_diagnosis"),
            )
            context = (await cur.fetchone())[0]
            assert context["schemaVersion"] == 2
            expected_binding = TOOL_REGISTRY.bind(tuple(context["allowedTools"])).snapshot
            assert all(context[key] == value for key, value in expected_binding.items())
            assert len(context["predecessors"]) == 2 and payload["question"] in context["task"]
            # Ordinary organization Trace contains protocol summaries, never the question.
            cur = await db.execute(
                "select count(*) from app.trace_artifact where run_id=(select trace_run_id from app.runtime_run where id=%s) and redacted_payload::text like %s",
                (run_id, "%" + payload["question"] + "%"),
            )
            assert (await cur.fetchone())[0] == 0
            await db.execute(
                "update app.crm_opportunity set scope=%s where id=%s",
                ('{"member_ids":[]}', opportunity),
            )
            await db.commit()
        assert (await client.get(path, params={"organization_id": str(org)})).status_code == 403
