from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from product_api.harness import (
    AgentEnginePort,
    HarnessContext,
    HarnessToolError,
    MemoryTraceSink,
    PiRuntimeAdapter,
    ToolDefinition,
    ToolRegistry,
    TraceSink,
    TurnResult,
)
from product_api.specialist_runner import (
    SpecialistResultError,
    SpecialistRunner,
    validate_knowledge_search,
)
from product_api.specialists import ClaimQuality, SpecialistAssignment, SpecialistRole

ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "pi_sidecar" / "f42_protocol_sidecar.mjs"


class ScriptedSpecialistEngine(AgentEnginePort):
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output

    async def run_turn(
        self,
        context: HarnessContext,
        tools: ToolRegistry,
        trace: TraceSink,
        cancel_event: asyncio.Event | None = None,
    ) -> TurnResult:
        del cancel_event
        assert context.fact_references[0].startswith("artifact:")
        await trace.record("specialist.turn_started", {"role": self.output["role"]}, 1)
        await tools.execute("knowledge.search", {"query": "MAX2837", "limit": 5})
        await trace.record("specialist.turn_completed", {}, 2)
        return TurnResult(uuid4(), "completed", ("knowledge.search",), 2, self.output)


def _citation() -> dict[str, object]:
    return {
        "sourceKind": "knowledge_chunk",
        "sourceId": str(uuid4()),
        "version": str(uuid4()),
        "locator": "section:hackrf-one-r9",
        "fragmentHash": "a" * 64,
    }


def _output(role: SpecialistRole = SpecialistRole.CUSTOMER_FACTS) -> dict[str, object]:
    return {
        "role": role.value,
        "complete": True,
        "claims": [
            {
                "subject": "opportunity-1",
                "claimType": "product_revision_fact",
                "conclusion": "HackRF r9 uses MAX2839 in place of MAX2837.",
                "quality": "supported",
                "citations": [_citation()],
                "unknowns": [],
                "conflicts": [],
            }
        ],
    }


def _assignment() -> SpecialistAssignment:
    return SpecialistAssignment(
        organization_id=uuid4(),
        mission_id=uuid4(),
        role=SpecialistRole.CUSTOMER_FACTS,
        task="读取已授权知识，确认产品版本事实。",
        input_artifact_ids=(uuid4(),),
        allowed_tools=("knowledge.search",),
    )


def _tools() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            "knowledge.search",
            lambda arguments: {"query": arguments["query"], "hits": []},
            validate_knowledge_search,
        ),
    )


def test_runner_forces_scope_tools_and_decodes_supported_claim() -> None:
    async def scenario() -> None:
        assignment = _assignment()
        trace = MemoryTraceSink()
        result = await SpecialistRunner(
            ScriptedSpecialistEngine(_output()), _tools(), trace, lambda _citation: True
        ).run(assignment)

        assert result.organization_id == assignment.organization_id
        assert result.mission_id == assignment.mission_id
        assert result.role is SpecialistRole.CUSTOMER_FACTS
        assert result.claims[0].quality is ClaimQuality.SUPPORTED
        assert result.claims[0].claim_id
        assert [event["eventType"] for event in trace.events] == [
            "specialist.turn_started",
            "specialist.turn_completed",
        ]

    asyncio.run(scenario())


def test_invalid_citation_is_downgraded_to_rejected() -> None:
    async def scenario() -> None:
        result = await SpecialistRunner(
            ScriptedSpecialistEngine(_output()),
            _tools(),
            MemoryTraceSink(),
            lambda _citation: False,
        ).run(_assignment())
        assert result.claims[0].quality is ClaimQuality.REJECTED
        assert result.claims[0].unknowns == ("citation_invalid",)

    asyncio.run(scenario())


def test_real_pi_replay_returns_a_validated_supply_risk_result() -> None:
    async def scenario() -> None:
        assignment = SpecialistAssignment(
            organization_id=uuid4(),
            mission_id=uuid4(),
            role=SpecialistRole.SUPPLY_RISK,
            task="调查 PART-A 的确定性缺料影响。",
            input_artifact_ids=(uuid4(),),
            allowed_tools=("supply.impact",),
        )
        tools = (
            ToolDefinition(
                "supply.impact",
                lambda arguments: {"part": arguments["partNumber"]},
            ),
        )
        result = await SpecialistRunner(
            PiRuntimeAdapter(("node", str(SIDECAR))),
            tools,
            MemoryTraceSink(),
            lambda _citation: True,
        ).run(assignment)

        assert result.role is SpecialistRole.SUPPLY_RISK
        assert result.claims[0].produced_by is SpecialistRole.SUPPLY_RISK
        assert result.claims[0].quality is ClaimQuality.SUPPORTED
        assert result.organization_id == assignment.organization_id
        assert result.mission_id == assignment.mission_id

    asyncio.run(scenario())


def test_assignment_tool_set_must_match_runtime_registry() -> None:
    assignment = _assignment()
    wrong = SpecialistAssignment(
        assignment.organization_id,
        assignment.mission_id,
        assignment.role,
        assignment.task,
        assignment.input_artifact_ids,
        ("supply.impact",),
    )
    with pytest.raises(SpecialistResultError, match="工具集合"):
        asyncio.run(
            SpecialistRunner(
                ScriptedSpecialistEngine(_output()),
                _tools(),
                MemoryTraceSink(),
                lambda _citation: True,
            ).run(wrong)
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": "MAX2837", "limit": 0},
        {"query": "MAX2837", "limit": True},
        {"query": "MAX2837", "unknown": "field"},
    ],
)
def test_knowledge_search_rejects_arguments_outside_fixed_schema(
    arguments: dict[str, object],
) -> None:
    with pytest.raises(HarnessToolError):
        asyncio.run(ToolRegistry(_tools()).execute("knowledge.search", arguments))


@pytest.mark.parametrize(
    "output",
    [
        {**_output(), "role": SpecialistRole.BUSINESS_POLICY.value},
        {**_output(), "unexpected": True},
        {**_output(), "claims": []},
    ],
)
def test_malformed_or_cross_role_output_is_rejected(output: dict[str, object]) -> None:
    with pytest.raises(SpecialistResultError):
        asyncio.run(
            SpecialistRunner(
                ScriptedSpecialistEngine(output),
                _tools(),
                MemoryTraceSink(),
                lambda _citation: True,
            ).run(_assignment())
        )
