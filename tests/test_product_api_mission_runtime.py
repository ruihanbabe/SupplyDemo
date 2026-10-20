from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from product_api.harness import (
    HarnessContext,
    MemoryTraceSink,
    ToolDefinition,
    ToolRegistry,
    TurnResult,
)
from product_api.mission_runtime import DurableMissionRunner
from product_api.specialist_runner import validate_knowledge_search, validate_no_arguments
from product_api.specialists import (
    IndependentReview,
    MissionQuality,
    ParentRecommendation,
    SpecialistResult,
    SpecialistRole,
)


def _citation() -> dict[str, object]:
    return {
        "sourceKind": "trace_artifact",
        "sourceId": str(uuid4()),
        "version": "1",
        "locator": "redacted_payload",
        "fragmentHash": "a" * 64,
    }


@dataclass(frozen=True)
class Job:
    mission_id: UUID = field(default_factory=uuid4)
    opportunity_id: UUID | None = field(default_factory=uuid4)
    case_id: UUID | None = field(default_factory=uuid4)
    part_number: str | None = "PART-A"


class RoleEngine:
    def __init__(self, fail_role: SpecialistRole | None = None) -> None:
        self.fail_role = fail_role

    async def run_turn(
        self,
        context: HarnessContext,
        tools: ToolRegistry,
        trace: MemoryTraceSink,
        cancel_event: asyncio.Event | None = None,
    ) -> TurnResult:
        del cancel_event
        role = SpecialistRole(json.loads(context.task)["role"])
        if role is self.fail_role:
            raise RuntimeError("fixture branch failure")
        name = tools.names[0]
        arguments = (
            {"query": "approval", "limit": 5}
            if name == "knowledge.search"
            else {"partNumber": "PART-A"}
            if name == "supply.impact"
            else {}
        )
        evidence = await tools.execute(name, arguments)
        await trace.record("fixture.completed", {"role": role.value}, 1)
        return TurnResult(
            uuid4(),
            "completed",
            (name,),
            1,
            {
                "role": role.value,
                "complete": True,
                "claims": [
                    {
                        "subject": str(context.run_id),
                        "claimType": f"{role.value}_fixture",
                        "conclusion": f"{role.value} completed.",
                        "quality": "supported",
                        "citations": [evidence["citation"]],
                        "unknowns": [],
                        "conflicts": [],
                    }
                ],
            },
        )


@dataclass
class Store:
    trace: MemoryTraceSink = field(default_factory=MemoryTraceSink)
    results: list[SpecialistResult] = field(default_factory=list)
    reviews: list[IndependentReview] = field(default_factory=list)
    failures: list[tuple[SpecialistRole, str]] = field(default_factory=list)
    parents: list[ParentRecommendation] = field(default_factory=list)

    async def mission_tools(self, job, role, input_artifact_ids):
        del job, input_artifact_ids
        names = {
            SpecialistRole.CUSTOMER_FACTS: "crm.opportunity_facts",
            SpecialistRole.BUSINESS_POLICY: "knowledge.search",
            SpecialistRole.SUPPLY_RISK: "supply.impact",
            SpecialistRole.OPPORTUNITY_DIAGNOSIS: "mission.claims",
            SpecialistRole.PROCUREMENT_STRATEGY: "mission.claims",
        }
        validator = (
            None
            if role is SpecialistRole.SUPPLY_RISK
            else validate_knowledge_search
            if role is SpecialistRole.BUSINESS_POLICY
            else validate_no_arguments
        )
        return (ToolDefinition(names[role], lambda _: {"citation": _citation()}, validator),)

    async def citation_valid(self, job, citation):
        del job
        return citation.source_kind == "trace_artifact"

    async def persist_specialist_result(self, job, result):
        del job
        self.results.append(result)
        return result.artifact_id

    async def persist_review(self, job, review):
        del job
        self.reviews.append(review)
        return uuid4()

    async def persist_specialist_failure(self, job, role, code):
        del job
        self.failures.append((role, code))
        return uuid4()

    async def persist_parent_recommendation(self, job, recommendation):
        del job
        self.parents.append(recommendation)
        return uuid4()

    def trace_sink(self, job):
        del job
        return self.trace


def test_durable_mission_runs_stages_reviews_and_one_parent() -> None:
    async def scenario() -> None:
        store = Store()
        parent = await DurableMissionRunner(store, uuid4(), lambda _: RoleEngine()).run(Job())
        assert [result.role for result in store.results] == [
            SpecialistRole.CUSTOMER_FACTS,
            SpecialistRole.BUSINESS_POLICY,
            SpecialistRole.SUPPLY_RISK,
            SpecialistRole.OPPORTUNITY_DIAGNOSIS,
        ]
        assert len(store.reviews) == 4 and not store.failures and len(store.parents) == 1
        assert parent.quality is MissionQuality.COMPLETE and parent.proposal_draft is not None

    asyncio.run(scenario())


def test_required_branch_failure_is_persisted_and_blocks_draft() -> None:
    async def scenario() -> None:
        store = Store()
        parent = await DurableMissionRunner(
            store, uuid4(), lambda _: RoleEngine(SpecialistRole.BUSINESS_POLICY)
        ).run(Job(case_id=None, part_number=None))
        assert store.failures == [(SpecialistRole.BUSINESS_POLICY, "RuntimeError")]
        assert parent.quality is MissionQuality.BLOCKED and parent.proposal_draft is None

    asyncio.run(scenario())
