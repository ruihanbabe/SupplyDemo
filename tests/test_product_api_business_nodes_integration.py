"""F46 story fixture -> real PostgreSQL/RLS -> durable Pi -> business observations."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Jsonb

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.business_nodes import FollowupPolicy, case_supply_context, followup_context
from product_api.crm import PostgresCrmFactRepository
from product_api.harness import PiRuntimeAdapter
from product_api.runtime import DurableWorker, PostgresRuntimeStore, WorkerIdentity, WorkerPolicy
from product_api.tenant import PostgresTenantRepository, TenantAccessError

ROOT = Path(__file__).resolve().parents[1]


def story_id(key):
    return uuid5(NAMESPACE_URL, "SupplyAgent/SYNTHETIC/electronics-followup-v1/" + key)


async def seed_story(db):
    # Seeds are deliberately restricted to throwaway clones, not the business database.
    cursor = await db.execute("select current_database()")
    if not (await cursor.fetchone())[0].startswith(("f46_scenario_", "f47_scenario_", "f49_scenario_")):
        raise ValueError("story seed requires an isolated f46_scenario_ clone")
    data = json.loads((ROOT / "data/scenarios/electronics/initial.json").read_text())
    assert data["schemaVersion"] == 1 and data["provenance"] == "synthetic"
    clock = datetime.fromisoformat(data["asOf"])
    org = story_id("org")
    await db.execute(
        "insert into app.organization(id,slug,display_name) values (%s,'f46-story',%s)",
        (org, data["organization"]),
    )
    for key, role in [
        ("sales", "sales_manager"),
        ("manager", "organization_admin"),
        ("worker", "maintainer"),
    ]:
        await db.execute(
            "insert into app.organization_member(organization_id,user_id,role,scope) values (%s,%s,%s,%s)",
            (org, story_id(key), role, Jsonb({"runtimeWorker": True} if key == "worker" else {})),
        )
    foreign = story_id("foreign-org")
    await db.execute(
        "insert into app.organization(id,slug,display_name) values (%s,'f46-foreign','SYNTHETIC Foreign')",
        (foreign,),
    )
    await db.execute(
        "insert into app.organization_member(organization_id,user_id,role) values (%s,%s,'organization_admin')",
        (foreign, story_id("foreign-user")),
    )
    case = story_id("case")
    await db.execute(
        "insert into app.supply_case(id,organization_id,title,status,severity,scope) values (%s,%s,'SYNTHETIC 澄川齐料调查','open','high',%s)",
        (case, org, Jsonb({"partNumbers": [p["mpn"] for p in data["parts"]]})),
    )
    for customer in data["customers"]:
        key = customer["key"]
        scope = Jsonb({"member_ids": [str(story_id(r)) for r in customer["readers"]]})
        await db.execute(
            "insert into app.crm_customer(id,organization_id,display_name,lifecycle,scope,created_by) values (%s,%s,%s,'active',%s,%s)",
            (story_id(key), org, customer["name"], scope, story_id("manager")),
        )
        await db.execute(
            "insert into app.crm_opportunity(id,organization_id,customer_id,title,stage,owner_id,supply_case_id,scope,created_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                story_id("opportunity/" + key),
                org,
                story_id(key),
                customer["opportunity"],
                customer["stage"],
                story_id(customer["owner"]),
                case if key == "chengchuan" else None,
                scope,
                story_id("manager"),
            ),
        )
    for item in data["tasks"]:
        customer = next(c for c in data["customers"] if c["key"] == item["customer"])
        await db.execute(
            "insert into app.crm_task(id,organization_id,opportunity_id,title,status,assignee_id,due_at,scope,created_by) values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                story_id(item["key"]),
                org,
                story_id("opportunity/" + item["customer"]),
                item["title"],
                item["status"],
                story_id(item["assignee"]) if item["assignee"] else None,
                clock + timedelta(days=item["dueDays"]) if item["dueDays"] is not None else None,
                Jsonb({"member_ids": [str(story_id(r)) for r in customer["readers"]]}),
                story_id("manager"),
            ),
        )
    for item in data["conversations"]:
        await db.execute(
            "insert into app.crm_conversation(id,organization_id,customer_id,opportunity_id,channel,summary,scope,created_by,created_at,updated_at) values (%s,%s,%s,%s,'manual',%s,%s,%s,%s,%s)",
            (
                story_id(item["key"]),
                org,
                story_id("chengchuan"),
                story_id("opportunity/chengchuan"),
                item["summary"],
                Jsonb({"member_ids": [str(story_id("sales")), str(story_id("manager"))]}),
                story_id("sales"),
                clock + timedelta(days=item["days"]),
                clock + timedelta(days=item["days"]),
            ),
        )
    for part in data["parts"]:
        mpn = part["mpn"]
        await db.execute(
            "insert into app.inventory_position(organization_id,part_number,location_code,on_hand_qty,reserved_qty,in_transit_qty,verified_at) values (%s,%s,'DEMO',%s,%s,0,%s)",
            (org, mpn, part["onHand"], part["reserved"], clock),
        )
        await db.execute(
            "insert into app.supply_demand(organization_id,source_type,source_reference,line_key,part_number,required_qty,required_on) values (%s,'customer_order','SYNTHETIC-CHENGCHUAN',%s,%s,%s,%s)",
            (
                org,
                part["lineKey"],
                mpn,
                data["board"]["sets"] * part["perSet"],
                (clock + timedelta(days=data["board"]["requiredDays"])).date(),
            ),
        )
        if receipt := part.get("confirmedReceipt"):
            await db.execute(
                "insert into app.supply_receipt(organization_id,receipt_reference,line_key,part_number,expected_on,quantity,status) values (%s,'SYNTHETIC-RECEIPT','1',%s,%s,%s,'in_transit')",
                (org, mpn, (clock + timedelta(days=receipt["days"])).date(), receipt["quantity"]),
            )
    await db.commit()
    return clock


@pytest.mark.integration
def test_business_story_reaches_private_node_observations(monkeypatch):
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL is not configured")
    monkeypatch.setenv("SUPPLYAGENT_BUSINESS_NODES_ENABLED", "1")
    asyncio.run(scenario(source))


async def scenario(source):
    name = "f46_scenario_" + uuid4().hex
    async with await AsyncConnection.connect(
        make_conninfo(source, dbname="postgres"), autocommit=True
    ) as admin:
        await admin.execute(
            sql.SQL("create database {} template {}").format(
                sql.Identifier(name), sql.Identifier(conninfo_to_dict(source)["dbname"])
            )
        )
        try:
            await run_in_clone(make_conninfo(source, dbname=name))
        finally:
            await admin.execute(
                sql.SQL("drop database {} with (force)").format(sql.Identifier(name))
            )


async def run_in_clone(url):
    async with await AsyncConnection.connect(url) as db:
        for filename in (
            "0024_f45_intelligence.sql",
            "0025_f46_business_nodes.sql",
            "0025_f46_business_nodes.sql",
            "0030_f51_tool_execution.sql",
            "0042_runtime_attempt_guard.sql",
            "0031_f51_tool_resources.sql",
        ):
            await db.execute((ROOT / "supabase/migrations" / filename).read_text())
        clock = await seed_story(db)
        with pytest.raises(ForeignKeyViolation):
            async with db.transaction():
                await db.execute(
                    "update app.crm_opportunity set owner_id=%s where id=%s",
                    (story_id("foreign-user"), story_id("opportunity/chengchuan")),
                )

    class Auth:
        async def verify_access_token(self, token):
            return AuthenticatedIdentity(story_id(token), "synthetic@example.invalid")

    org = story_id("org")
    tenants = PostgresTenantRepository(url)
    app = create_app(auth_verifier=Auth(), tenant_repository=tenants)
    identity = WorkerIdentity(org, story_id("worker"), "f46-worker")
    runtime = DurableWorker(
        PostgresRuntimeStore(tenants, identity),
        identity,
        WorkerPolicy(60, 5, 1, 0),
        lambda job: PiRuntimeAdapter(
            ("node", str(ROOT / "pi_sidecar/f42_protocol_sidecar.mjs")), timeout_seconds=20
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"authorization": "Bearer sales"},
    ) as client:
        choices = await client.get(
            "/v1/intelligence/opportunities", params={"organization_id": str(org)}
        )
        assert len(choices.json()["data"]) == 1
        payload = {
            "organization_id": str(org),
            "opportunity_id": str(story_id("opportunity/chengchuan")),
            "question": "请检查跟进缺口和齐料风险",
            "idempotency_key": "f46-story",
            "as_of": clock.isoformat(),
        }
        response = await client.post("/v1/intelligence/runs", json=payload)
        assert response.status_code == 202, response.text
        assert (await client.post("/v1/intelligence/runs", json=payload)).json() == response.json()
        assert (
            await client.post(
                "/v1/intelligence/runs",
                json={**payload, "as_of": (clock + timedelta(days=1)).isoformat()},
            )
        ).status_code == 409
        assert (
            await client.post(
                "/v1/intelligence/runs",
                json={
                    **payload,
                    "opportunity_id": str(story_id("opportunity/hean")),
                    "idempotency_key": "foreign-scope",
                },
            )
        ).status_code == 403
        assert (
            await client.get(
                "/v1/intelligence/runs/" + response.json()["data"]["runId"],
                params={"organization_id": str(org)},
                headers={"authorization": "Bearer foreign-user"},
            )
        ).status_code == 403
        # Switching intake off must not downgrade an already accepted run.
        os.environ["SUPPLYAGENT_BUSINESS_NODES_ENABLED"] = "0"
        assert await runtime.run_once()
        run_id = response.json()["data"]["runId"]
        answer = (
            await client.get(
                "/v1/intelligence/runs/" + run_id, params={"organization_id": str(org)}
            )
        ).json()["data"]
        assert answer["status"] == "completed", answer
        assert answer["businessNodesEnabled"] is True
        assert datetime.fromisoformat(answer["asOf"]) == clock
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='crm.opportunity_facts'",
                (response.json()["data"]["missionId"],),
            )
            crm = (await cursor.fetchone())[0]
            assert {s["code"] for s in crm["followup"]["signals"]} == {
                "task_overdue",
                "no_visible_future_task",
            }
            assert crm["followup"]["policy"]["policy_id"] == "SYNTHETIC-DEMO-FOLLOWUP-v1"
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='supply.bound_impact'",
                (response.json()["data"]["missionId"],),
            )
            supply = (await cursor.fetchone())[0]
            assert supply["parts"] == ["SIM-MCU-01", "SIM-PWR-01", "SIM-RF-SW-01"]
            assert {i["part_number"]: i["projected_available_qty"] for i in supply["impacts"]} == {
                "SIM-MCU-01": "150.0000",
                "SIM-PWR-01": "300.0000",
                "SIM-RF-SW-01": "-200.0000",
            }
            cursor = await db.execute(
                "select input_context from app.intelligence_attempt where mission_id=%s",
                (response.json()["data"]["missionId"],),
            )
            contexts = await cursor.fetchall()
            assert all(datetime.fromisoformat(c[0]["analysisAt"]) == clock for c in contexts)
            # This is an external fixture transition, not an implemented F41 approval.
            await db.execute(
                "insert into app.crm_task(id,organization_id,opportunity_id,title,status,assignee_id,due_at,scope,created_by) values (%s,%s,%s,'SYNTHETIC 确认追加供应','open',%s,%s,%s,%s)",
                (
                    story_id("T2"),
                    org,
                    story_id("opportunity/chengchuan"),
                    story_id("sales"),
                    clock + timedelta(days=1),
                    Jsonb({"member_ids": [str(story_id("sales")), str(story_id("manager"))]}),
                    story_id("manager"),
                ),
            )
            # A future task invisible to sales cannot be counted as their visible next step.
            await db.execute(
                "update app.crm_task set scope=%s where id=%s",
                (Jsonb({"member_ids": [str(story_id("manager"))]}), story_id("T2")),
            )
            await db.commit()

        async def current(c):
            facts = await PostgresCrmFactRepository().load_opportunity_facts(
                c, story_id("opportunity/chengchuan")
            )
            return await followup_context(c, facts, clock, FollowupPolicy.load())

        sales_view = await tenants.in_organization(story_id("sales"), org, current)
        manager_view = await tenants.in_organization(story_id("manager"), org, current)
        assert {s["code"] for s in sales_view["signals"]} == {
            "task_overdue",
            "no_visible_future_task",
        }
        assert {s["code"] for s in manager_view["signals"]} == {"task_overdue"}
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.crm_task set scope=%s where id=%s",
                (
                    Jsonb({"member_ids": [str(story_id("sales")), str(story_id("manager"))]}),
                    story_id("T2"),
                ),
            )
            await db.commit()
        assert {
            s["code"]
            for s in (await tenants.in_organization(story_id("sales"), org, current))["signals"]
        } == {"task_overdue"}
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select status from app.crm_task where id=%s", (story_id("T1"),)
            )
            assert (await cursor.fetchone())[0] == "open"

        os.environ["SUPPLYAGENT_BUSINESS_NODES_ENABLED"] = "1"
        later = await client.post(
            "/v1/intelligence/runs", json={**payload, "idempotency_key": "f46-after-T2"}
        )
        assert later.status_code == 202, later.text
        assert await runtime.run_once()
        later_answer = (
            await client.get(
                "/v1/intelligence/runs/" + later.json()["data"]["runId"],
                params={"organization_id": str(org)},
            )
        ).json()["data"]
        assert later_answer["status"] == "completed", later_answer
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='crm.opportunity_facts'",
                (later.json()["data"]["missionId"],),
            )
            updated = (await cursor.fetchone())[0]
            assert {s["code"] for s in updated["followup"]["signals"]} == {"task_overdue"}
            assert {s["code"] for s in crm["followup"]["signals"]} == {
                "task_overdue",
                "no_visible_future_task",
            }
        bound = ("SIM-MCU-01", "SIM-PWR-01", "SIM-RF-SW-01")

        async def midnight_projection(c):
            return await case_supply_context(
                c,
                story_id("opportunity/chengchuan"),
                story_id("case"),
                clock.replace(hour=0, minute=30),
                FollowupPolicy.load(),
                bound,
            )

        projected = await tenants.in_organization(story_id("sales"), org, midnight_projection)
        assert all(impact["as_of"] == clock.date() for impact in projected["impacts"])
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.supply_case set scope=%s where id=%s",
                (Jsonb({"partNumbers": [*bound, "SIM-UNBOUND"]}), story_id("case")),
            )
            await db.commit()
        with pytest.raises(TenantAccessError, match="scope changed"):
            await tenants.in_organization(story_id("sales"), org, midnight_projection)
