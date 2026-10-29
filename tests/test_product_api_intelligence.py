"""F45 bounded input, no paid-provider switch, and exact turn identity."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity
from product_api.harness import MemoryTraceSink, ToolDefinition, TurnResult
from product_api.specialist_runner import SpecialistRunner, validate_no_arguments
from product_api.specialists import SpecialistAssignment, SpecialistRole
from product_api.tenant import UnavailableTenantRepository


class Auth:
    async def verify_access_token(self, token):
        return AuthenticatedIdentity(uuid4(), "synthetic@example.invalid")


def test_question_entry_cannot_enable_paid_models_and_can_be_ablated(monkeypatch):
    client = TestClient(
        create_app(auth_verifier=Auth(), tenant_repository=UnavailableTenantRepository())
    )
    body = {
        "organization_id": str(uuid4()),
        "opportunity_id": str(uuid4()),
        "question": "跟进有哪些缺口？",
        "idempotency_key": "f45-test",
    }
    headers = {"authorization": "Bearer synthetic"}
    for update in (
        {"mode": "gateway"},
        {"question": " "},
        {"question": "长" * 601},
        {"as_of": "2026-10-23T09:00:00"},
    ):
        assert (
            client.post(
                "/v1/intelligence/runs", json={**body, **update}, headers=headers
            ).status_code
            == 422
        )
    monkeypatch.setenv("SUPPLYAGENT_RESPONSES_ENABLED", "0")
    assert (
        client.post(
            "/v1/intelligence/runs",
            json={**body, "mode": "gateway", "model_policy_id": str(uuid4())},
            headers=headers,
        ).status_code
        == 503
    )
    assert (
        client.post(
            "/v1/intelligence/runs", json={**body, "model_policy_id": str(uuid4())}, headers=headers
        ).status_code
        == 422
    )
    monkeypatch.setenv("SUPPLYAGENT_BUSINESS_NODES_ENABLED", "0")
    # No silent fallback to today's clock when a caller explicitly selected one.
    assert (
        client.post(
            "/v1/intelligence/runs",
            json={**body, "as_of": "2026-10-23T09:00:00+08:00"},
            headers=headers,
        ).status_code
        == 503
    )
    monkeypatch.setenv("SUPPLYAGENT_INTELLIGENCE_ENABLED", "0")
    assert client.post("/v1/intelligence/runs", json=body, headers=headers).status_code == 503


def test_specialist_keeps_actual_run_invocation_and_role_instructions():
    asyncio.run(identity_scenario())


async def identity_scenario():
    org, mission, run, invocation = (uuid4() for _ in range(4))

    class Engine:
        async def run_turn(self, context, tools, trace, cancel_event=None):
            assert context.run_id == run and context.invocation_id == invocation
            assert context.instructions == "Trusted role instructions"
            assert context.task == "Untrusted question data"
            assert tools.names == ("crm.opportunity_facts",)
            return TurnResult(
                invocation,
                "completed",
                (),
                0,
                {
                    "role": "customer_facts",
                    "complete": True,
                    "claims": [
                        {
                            "subject": "opportunity",
                            "claimType": "fact",
                            "conclusion": "无法确认",
                            "quality": "unsupported",
                            "citations": [],
                            "unknowns": ["缺少证据"],
                            "conflicts": [],
                        }
                    ],
                },
            )

    assignment = SpecialistAssignment(
        org,
        mission,
        SpecialistRole.CUSTOMER_FACTS,
        "Untrusted question data",
        (),
        ("crm.opportunity_facts",),
        run,
        invocation,
        "Trusted role instructions",
    )
    result = await SpecialistRunner(
        Engine(),
        (ToolDefinition("crm.opportunity_facts", lambda args: {}, validate_no_arguments),),
        MemoryTraceSink(),
        lambda citation: False,
    ).run(assignment)
    assert result.mission_id == mission and result.claims[0].unknowns == ("缺少证据",)
