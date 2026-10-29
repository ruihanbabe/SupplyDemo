"""F47 synthetic fixtures in a disposable PostgreSQL clone and real Pi Replay."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import ForeignKeyViolation, InvalidParameterValue
from psycopg.types.json import Jsonb
from test_product_api_business_nodes_integration import seed_story, story_id

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.harness import PiRuntimeAdapter
from product_api.runtime import DurableWorker, PostgresRuntimeStore, WorkerIdentity, WorkerPolicy
from product_api.tenant import PostgresTenantRepository

ROOT = Path(__file__).resolve().parents[1]


async def seed_commercial(db, clock):
    """Privileged fixture ingestion, never a product approval or channel action."""
    org = story_id("org")
    readers = Jsonb({"member_ids": [str(story_id(k)) for k in ("sales", "manager")]})
    await db.execute(
        "insert into app.catalog_product(id,organization_id,code,name,scope) values (%s,%s,'SYNTHETIC-RF-100','SYNTHETIC RF board',%s)",
        (story_id("product"), org, readers),
    )
    for revision, rf_quantity in (("A", "1"), ("B", "2")):
        lines = [
            {"lineKey": key, "manufacturer": "SYNTHETIC", "mpn": mpn, "quantityPerUnit": qty}
            for key, mpn, qty in (
                ("1", "SIM-MCU-01", "1"),
                ("2", "SIM-RF-SW-01", rf_quantity),
                ("3", "SIM-PWR-01", "2"),
            )
        ]
        await db.execute(
            "insert into app.bom_revision(id,organization_id,product_id,revision,lines,source_kind,source_reference) values (%s,%s,%s,%s,%s,'synthetic','SYNTHETIC-F47')",
            (story_id("bom/" + revision), org, story_id("product"), revision, Jsonb(lines)),
        )
    await db.execute(
        "insert into app.opportunity_bom_binding(organization_id,opportunity_id,bom_revision_id,units,required_on) values (%s,%s,%s,500,%s)",
        (
            org,
            story_id("opportunity/chengchuan"),
            story_id("bom/A"),
            (clock + timedelta(days=7)).date(),
        ),
    )
    for key, kind, line, expiry, scope in (
        ("supplier", "supplier_offer", "2", 1, readers),
        ("customer", "customer_offer", None, -1, readers),
        ("hidden", "customer_offer", None, 1, Jsonb({"member_ids": [str(story_id("manager"))]})),
    ):
        await db.execute(
            "insert into app.commercial_quote_revision(id,organization_id,opportunity_id,bom_revision_id,reference,revision,quote_kind,bom_line_key,quantity,unit_price,currency,issued_at,valid_until,estimated_ship_on,scope,source_kind) values (%s,%s,%s,%s,%s,1,%s,%s,200,1.250001,'USD',%s,%s,%s,%s,'synthetic')",
            (
                story_id("quote/" + key),
                org,
                story_id("opportunity/chengchuan"),
                story_id("bom/A"),
                "SYNTHETIC-" + key,
                kind,
                line,
                clock - timedelta(days=2),
                clock + timedelta(days=expiry),
                (clock + timedelta(days=6)).date(),
                scope,
            ),
        )
    await db.commit()


@pytest.mark.integration
def test_bom_and_quote_specialist_end_to_end(monkeypatch):
    load_dotenv(".env")
    source = os.getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    if not source:
        pytest.skip("local product PostgreSQL is not configured")
    monkeypatch.setenv("SUPPLYAGENT_COMMERCIAL_NODES_ENABLED", "1")
    asyncio.run(scenario(source))


async def scenario(source):
    name = "f47_scenario_" + uuid4().hex
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
            "0026_f47_bom_quotes.sql",
            "0026_f47_bom_quotes.sql",
            "0030_f51_tool_execution.sql",
            "0042_runtime_attempt_guard.sql",
            "0031_f51_tool_resources.sql",
        ):
            await db.execute((ROOT / "supabase/migrations" / filename).read_text())
        clock = await seed_story(db)
        await seed_commercial(db, clock)
        for statement, params, error in (
            (
                "update app.bom_revision set lines='[]' where id=%s",
                (story_id("bom/A"),),
                InvalidParameterValue,
            ),
            (
                "update app.commercial_quote_revision set unit_price=2 where id=%s",
                (story_id("quote/supplier"),),
                InvalidParameterValue,
            ),
            (
                "update app.opportunity_bom_binding set organization_id=%s where opportunity_id=%s",
                (story_id("foreign-org"), story_id("opportunity/chengchuan")),
                InvalidParameterValue,
            ),
            (
                "insert into app.opportunity_bom_binding(organization_id,opportunity_id,bom_revision_id,units,required_on) values (%s,%s,%s,1,current_date)",
                (story_id("foreign-org"), story_id("opportunity/hean"), story_id("bom/A")),
                ForeignKeyViolation,
            ),
        ):
            with pytest.raises(error):
                async with db.transaction():
                    await db.execute(statement, params)
        for lines in (
            [
                {
                    "lineKey": "1",
                    "manufacturer": "SYNTHETIC",
                    "mpn": "SIM-RF-SW-01",
                    "quantityPerUnit": "NaN",
                }
            ],
            [
                {
                    "lineKey": "1",
                    "manufacturer": "SYNTHETIC",
                    "mpn": "SIM-RF-SW-01",
                    "quantityPerUnit": "0.0000001",
                }
            ],
        ):
            with pytest.raises(InvalidParameterValue):
                async with db.transaction():
                    await db.execute(
                        "insert into app.bom_revision(organization_id,product_id,revision,lines,source_kind,source_reference) values (%s,%s,'invalid',%s,'synthetic','SYNTHETIC-invalid')",
                        (story_id("org"), story_id("product"), Jsonb(lines)),
                    )
        with pytest.raises(ForeignKeyViolation):
            async with db.transaction():
                await db.execute(
                    "insert into app.commercial_quote_revision(organization_id,opportunity_id,bom_revision_id,reference,revision,quote_kind,bom_line_key,quantity,issued_at,source_kind) values (%s,%s,%s,'invalid',1,'supplier_offer','outside-BOM',1,%s,'synthetic')",
                    (story_id("org"), story_id("opportunity/chengchuan"), story_id("bom/A"), clock),
                )
        with pytest.raises(InvalidParameterValue):
            async with db.transaction():
                await db.execute(
                    "delete from app.opportunity_bom_binding where opportunity_id=%s",
                    (story_id("opportunity/chengchuan"),),
                )
        # Contradictory old manual demand must not be added to BOM-derived demand.
        await db.execute(
            "update app.supply_demand set required_qty=9999 where organization_id=%s",
            (story_id("org"),),
        )
        await db.commit()

    class Auth:
        async def verify_access_token(self, token):
            return AuthenticatedIdentity(story_id(token), "synthetic@example.invalid")

    org = story_id("org")
    tenants = PostgresTenantRepository(url)
    app = create_app(auth_verifier=Auth(), tenant_repository=tenants)
    identity = WorkerIdentity(org, story_id("worker"), "f47-worker")
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
        payload = {
            "organization_id": str(org),
            "opportunity_id": str(story_id("opportunity/chengchuan")),
            "question": "核验 BOM 需求与报价时效",
            "idempotency_key": "f47-A",
            "as_of": clock.isoformat(),
        }

        async def submit(key):
            response = await client.post(
                "/v1/intelligence/runs", json={**payload, "idempotency_key": key}
            )
            assert response.status_code == 202, response.text
            return response.json()["data"]

        async def answer(run):
            response = await client.get(
                "/v1/intelligence/runs/" + run["runId"], params={"organization_id": str(org)}
            )
            assert response.status_code == 200, response.text
            return response.json()["data"]

        first = await submit("f47-A")
        assert await submit("f47-A") == first
        os.environ["SUPPLYAGENT_COMMERCIAL_NODES_ENABLED"] = "0"
        assert await runtime.run_once()
        os.environ["SUPPLYAGENT_COMMERCIAL_NODES_ENABLED"] = "1"
        result = await answer(first)
        assert result["status"] == "completed", result
        assert result["commercialNodesEnabled"] is True
        assert result["result"] is not None
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select role,status from app.intelligence_attempt where mission_id=%s",
                (first["missionId"],),
            )
            attempts = await cursor.fetchall()
            assert len(attempts) == 5, attempts
            assert ("quote_validity", "completed") in attempts
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='commercial.quote_validity'",
                (first["missionId"],),
            )
            quotes = (await cursor.fetchone())[0]
            by_ref = {q["reference"]: q for q in quotes["quotes"]}
            assert set(by_ref) == {"SYNTHETIC-supplier", "SYNTHETIC-customer"}
            assert by_ref["SYNTHETIC-supplier"]["validity"] == "within_validity_window"
            assert by_ref["SYNTHETIC-supplier"]["unit_price"] == "1.250001"
            assert by_ref["SYNTHETIC-supplier"]["confirmed_arrival_on"] is None
            assert by_ref["SYNTHETIC-customer"]["validity"] == "expired"
            assert quotes["bom"]["revision"] == "A"  # B already exists, never auto-select latest.
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='supply.bound_impact'",
                (first["missionId"],),
            )
            supply = (await cursor.fetchone())[0]
            impacts = {x["part_number"]: x for x in supply["impacts"]}
            assert Decimal(impacts["SIM-RF-SW-01"]["projected_available_qty"]) == Decimal(-200)
            assert supply["calculationScope"] == "bound_opportunity_bom"

        # Quote ACL is rechecked when reading a previously completed answer.
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.commercial_quote_revision set scope=%s where id=%s",
                (Jsonb({"member_ids": [str(story_id("manager"))]}), story_id("quote/supplier")),
            )
            await db.commit()
        hidden = await answer(first)
        assert hidden["result"] is None and hidden["evidence"] == []
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.commercial_quote_revision set scope=%s where id=%s",
                (
                    Jsonb({"member_ids": [str(story_id("manager")), str(story_id("sales"))]}),
                    story_id("quote/supplier"),
                ),
            )
            await db.commit()
        assert (await answer(first))["result"] is not None

        # BOM access follows its product ACL, including old result reads.
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.catalog_product set scope=%s where id=%s",
                (Jsonb({"member_ids": [str(story_id("manager"))]}), story_id("product")),
            )
            await db.commit()
        assert (await answer(first))["result"] is None
        blocked = await client.post(
            "/v1/intelligence/runs", json={**payload, "idempotency_key": "f47-hidden-bom"}
        )
        assert blocked.status_code == 403, blocked.text
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.catalog_product set scope=%s where id=%s",
                (
                    Jsonb({"member_ids": [str(story_id("manager")), str(story_id("sales"))]}),
                    story_id("product"),
                ),
            )
            await db.commit()
        # A CRM-only opportunity still gets quote investigation, with explicit missing BOM.
        no_case_response = await client.post(
            "/v1/intelligence/runs",
            json={
                **payload,
                "opportunity_id": str(story_id("opportunity/hean")),
                "idempotency_key": "f47-no-case",
            },
            headers={"authorization": "Bearer manager"},
        )
        assert no_case_response.status_code == 202, no_case_response.text
        no_case = no_case_response.json()["data"]
        assert await runtime.run_once()
        no_case_answer = await client.get(
            "/v1/intelligence/runs/" + no_case["runId"],
            params={"organization_id": str(org)},
            headers={"authorization": "Bearer manager"},
        )
        assert no_case_answer.json()["data"]["status"] == "completed", no_case_answer.text
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select role from app.intelligence_attempt where mission_id=%s",
                (no_case["missionId"],),
            )
            assert {r[0] for r in await cursor.fetchall()} == {
                "customer_facts",
                "business_policy",
                "quote_validity",
                "opportunity_diagnosis",
            }
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='commercial.quote_validity'",
                (no_case["missionId"],),
            )
            missing = (await cursor.fetchone())[0]
            assert missing["bom"] is None and missing["quotes"] == [] and missing["unknowns"]

        stale = await submit("f47-stale")
        async with await AsyncConnection.connect(url) as db:
            await db.execute(
                "update app.opportunity_bom_binding set bom_revision_id=%s where opportunity_id=%s",
                (story_id("bom/B"), story_id("opportunity/chengchuan")),
            )
            await db.commit()
        assert await runtime.run_once()
        assert (await answer(stale))["status"] == "failed"
        historic = await answer(first)
        assert historic["result"] is not None
        assert "bom_binding_changed_since_run" in historic["issues"]
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select count(*) from app.intelligence_attempt where mission_id=%s",
                (stale["missionId"],),
            )
            assert (await cursor.fetchone())[0] == 0
        revised = await submit("f47-B")
        assert await runtime.run_once()
        assert (await answer(revised))["status"] == "completed"
        async with await AsyncConnection.connect(url) as db:
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='supply.bound_impact'",
                (revised["missionId"],),
            )
            supply_b = (await cursor.fetchone())[0]
            rf = next(x for x in supply_b["impacts"] if x["part_number"] == "SIM-RF-SW-01")
            assert Decimal(rf["projected_available_qty"]) == Decimal(-700)
            cursor = await db.execute(
                "select payload from app.intelligence_observation where mission_id=%s and tool_name='commercial.quote_validity'",
                (revised["missionId"],),
            )
            assert {q["validity"] for q in (await cursor.fetchone())[0]["quotes"]} == {
                "bom_mismatch"
            }
            await db.execute(
                "update app.commercial_quote_revision set revoked_at=%s where id=%s",
                (clock, story_id("quote/supplier")),
            )
            await db.commit()
        withdrawn = await answer(first)
        assert withdrawn["result"] is None, withdrawn
        assert (
            await client.get(
                "/v1/intelligence/runs/" + first["runId"],
                params={"organization_id": str(org)},
                headers={"authorization": "Bearer foreign-user"},
            )
        ).status_code == 403
