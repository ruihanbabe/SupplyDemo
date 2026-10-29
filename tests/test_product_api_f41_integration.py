"""F41 public HTTP/RLS seam in an isolated PostgreSQL clone."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.tenant import PostgresTenantRepository


@pytest.mark.integration
@pytest.mark.parametrize("fault", [None, "target_changed", "opt_out", "policy_changed", "approver_revoked", "requester_revoked", "content_changed", "expired"])
def test_proposal_review_requires_same_organization_and_human_capability(fault) -> None:
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL URL not configured")
    asyncio.run(_scenario(source, fault))


async def _scenario(source: str, fault=None) -> None:
    clone_name = f"f41_test_{uuid4().hex}"
    admin_url = make_conninfo(source, dbname="postgres")
    clone_url = make_conninfo(source, dbname=clone_name)
    async with await AsyncConnection.connect(admin_url, autocommit=True) as admin:
        await admin.execute(sql.SQL("create database {} template {}").format(
            sql.Identifier(clone_name), sql.Identifier(conninfo_to_dict(source)["dbname"]),
        ))
        try:
            async with await AsyncConnection.connect(clone_url) as connection:
                await connection.execute(
                    (Path(__file__).resolve().parents[1] / "supabase/migrations/0023_f41_proposals.sql").read_text()
                )
                for filename in ("0030_f51_tool_execution.sql", "0031_f51_tool_resources.sql", "0042_runtime_attempt_guard.sql", "0043_f41_execution.sql"):
                    await connection.execute((Path(__file__).resolve().parents[1] / "supabase/migrations" / filename).read_text())
                await connection.commit()
            await _run_in_clone(clone_url, fault)
        finally:
            await admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(clone_name)))


async def _run_in_clone(database_url: str, fault=None) -> None:
    organization_id, other_id = uuid4(), uuid4()
    proposer_id, approver_id, outsider_id = uuid4(), uuid4(), uuid4()
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            for org_id in (organization_id, other_id):
                await cursor.execute(
                    "insert into app.organization(id,slug,display_name) values (%s,%s,'F41 test')",
                    (org_id, f"f41-{org_id.hex}"),
                )
            for actor_id, org_id, role in (
                (proposer_id, organization_id, "sales_manager"),
                (approver_id, organization_id, "approver"),
                (outsider_id, other_id, "organization_admin"),
            ):
                await cursor.execute(
                    "insert into app.organization_member(organization_id,user_id,role) values (%s,%s,%s)",
                    (org_id, actor_id, role),
                )
            await cursor.execute(
                "insert into app.crm_customer(organization_id,display_name,lifecycle,created_by) values (%s,'SYNTHETIC buyer','active',%s) returning id",
                (organization_id, proposer_id),
            )
            customer_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.trace_run(organization_id,run_kind,actor_id) values (%s,'manual',%s) returning id",
                (organization_id, proposer_id),
            )
            trace_run_id = (await cursor.fetchone())[0]
            await cursor.execute("select app.set_request_context(%s,%s)", (proposer_id, organization_id))
            await cursor.execute(
                "select app.append_trace_artifact(%s,null,%s,'manual_action_evidence','SYNTHETIC evidence','{}') as id",
                (trace_run_id, uuid4()),
            )
            evidence_id = (await cursor.fetchone())[0]
            await cursor.execute(
                "insert into app.action_service_policy(organization_id,revision,enabled) values(%s,1,true)",
                (organization_id,),
            )
        await connection.commit()

    class FixedAuth:
        async def verify_access_token(self, token: str) -> AuthenticatedIdentity:
            return AuthenticatedIdentity({
                "proposer": proposer_id, "approver": approver_id, "outsider": outsider_id,
            }[token], "fixture@example.test")

    app = create_app(FixedAuth(), PostgresTenantRepository(database_url))
    headers = lambda actor: {"authorization": f"Bearer {actor}"}
    payload = {
        "idempotencyKey": "f41-request",
        "organizationId": str(organization_id),
        "actionKind": "customer_update",
        "targetId": str(customer_id),
        "targetVersion": 1,
        "content": {"displayName": "SYNTHETIC updated buyer"},
        "evidenceArtifactIds": [str(evidence_id)],
        "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        forbidden = await client.post("/v1/proposals", json=payload, headers=headers("outsider"))
        assert forbidden.status_code == 403
        created = await client.post("/v1/proposals", json=payload, headers=headers("proposer"))
        assert created.status_code == 200, created.text
        proposal_id = created.json()["data"]["proposalId"]
        assert created.json()["data"]["status"] == "pending_review"
        retried = await client.post("/v1/proposals", json=payload, headers=headers("proposer"))
        assert retried.status_code == 200 and retried.json()["data"]["proposalId"] == proposal_id
        conflict = await client.post("/v1/proposals", json={**payload, "content": {"displayName": "other"}}, headers=headers("proposer"))
        assert conflict.status_code == 409
        proposer_review = await client.post(
            f"/v1/proposals/{proposal_id}/review",
            json={"organizationId": str(organization_id), "decision": "approved"},
            headers=headers("proposer"),
        )
        assert proposer_review.status_code == 403
        approved = await client.post(
            f"/v1/proposals/{proposal_id}/review",
            json={"organizationId": str(organization_id), "decision": "approved"},
            headers=headers("approver"),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["data"]["status"] == "approved"
        detail = await client.get(f"/v1/proposals/{proposal_id}", params={"organization_id": str(organization_id)}, headers=headers("approver"))
        assert detail.status_code == 200 and detail.json()["data"]["content"] == payload["content"]
        assert "authorization_id" not in detail.json()["data"]
        repeated = await client.post(
            f"/v1/proposals/{proposal_id}/review",
            json={"organizationId": str(organization_id), "decision": "rejected"},
            headers=headers("approver"),
        )
        assert repeated.status_code == 409
        async with await AsyncConnection.connect(database_url) as db:
            worker_id = uuid4()
            await db.execute(
                "insert into app.organization_member(organization_id,user_id,role,scope) values(%s,%s,'maintainer','{\"runtimeWorker\":true}')",
                (organization_id, worker_id),
            )
            if fault == "target_changed":
                await db.execute("update app.crm_customer set version=version+1 where id=%s", (customer_id,))
            elif fault == "opt_out":
                await db.execute("update app.crm_customer set action_opt_out=true where id=%s", (customer_id,))
            elif fault == "policy_changed":
                await db.execute("update app.action_service_policy set revision=2 where organization_id=%s", (organization_id,))
            elif fault == "approver_revoked":
                await db.execute("update app.organization_member set role='sales_manager' where user_id=%s", (approver_id,))
            elif fault == "requester_revoked":
                await db.execute("update app.organization_member set role='auditor' where user_id=%s", (proposer_id,))
            elif fault == "content_changed":
                await db.execute("update app.action_proposal set content='{\"displayName\":\"tampered\"}' where id=%s", (proposal_id,))
            elif fault == "expired":
                await db.execute("update app.action_proposal set expires_at=clock_timestamp()-interval '1 second' where id=%s", (proposal_id,))
        from product_api.runtime import (
            DurableWorker,
            PostgresRuntimeStore,
            WorkerIdentity,
            WorkerPolicy,
        )

        identity = WorkerIdentity(organization_id, worker_id, "f41-execution")
        store = PostgresRuntimeStore(PostgresTenantRepository(database_url), identity)

        def no_model(job):
            raise AssertionError("approved execution must never invoke a model")

        worker = DurableWorker(store, identity, WorkerPolicy(30, 5, 3, 0), no_model)
        assert (await worker.run_once_report()).outcome == ("failed" if fault else "completed")
        assert (await worker.run_once_report()).outcome == "idle"
        async with await AsyncConnection.connect(database_url) as db:
            row = await (await db.execute("select display_name,version from app.crm_customer where id=%s", (customer_id,))).fetchone()
            if fault:
                assert row[0] == "SYNTHETIC buyer"
                assert (await (await db.execute("select state from app.action_execution where proposal_id=%s", (proposal_id,))).fetchone())[0] == "rejected"
                return
            assert row == ("SYNTHETIC updated buyer", 2)
            ledger = await (await db.execute("select state,job_id,trace_run_id from app.action_execution where proposal_id=%s", (proposal_id,))).fetchone()
            assert ledger[0] == "succeeded" and ledger[2] != trace_run_id
            # Replay a committed side effect after a crash before Job completion.
            await db.execute("update app.runtime_job set status='queued',lease_owner=null,lease_expires_at=null where id=%s", (ledger[1],))
            await db.execute("update app.runtime_run set status='queued' where id=(select run_id from app.runtime_job where id=%s)", (ledger[1],))
        assert (await worker.run_once_report()).outcome == "completed"
        async with await AsyncConnection.connect(database_url) as db:
            assert (await (await db.execute("select version from app.crm_customer where id=%s", (customer_id,))).fetchone())[0] == 2
