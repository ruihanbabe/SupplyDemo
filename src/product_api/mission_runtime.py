"""F36 durable multi-Specialist Mission orchestration over F35/F40/F42 ports."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from product_api.harness import AgentEnginePort, ToolDefinition, TraceSink
from product_api.specialist_runner import SpecialistRunner
from product_api.specialists import (
    IndependentReview,
    MissionPlan,
    ParentAgent,
    ParentRecommendation,
    ReviewVerdict,
    SpecialistAssignment,
    SpecialistResult,
    SpecialistRole,
)


class MissionRuntimeJob(Protocol):
    mission_id: UUID
    opportunity_id: UUID | None
    case_id: UUID | None
    part_number: str | None


class MissionRuntimeStore(Protocol):
    async def mission_tools(
        self,
        job: MissionRuntimeJob,
        role: SpecialistRole,
        input_artifact_ids: tuple[UUID, ...],
    ) -> tuple[ToolDefinition, ...]: ...
    async def citation_valid(self, job: MissionRuntimeJob, citation: object) -> bool: ...
    async def persist_specialist_result(
        self, job: MissionRuntimeJob, result: SpecialistResult
    ) -> UUID: ...
    async def persist_review(self, job: MissionRuntimeJob, review: IndependentReview) -> UUID: ...
    async def persist_specialist_failure(
        self, job: MissionRuntimeJob, role: SpecialistRole, code: str
    ) -> UUID: ...
    async def persist_parent_recommendation(
        self, job: MissionRuntimeJob, recommendation: ParentRecommendation
    ) -> UUID: ...
    def trace_sink(self, job: MissionRuntimeJob) -> TraceSink: ...


EngineFactory = Callable[[MissionRuntimeJob], AgentEnginePort]


class DurableMissionRunner:
    """Run stage branches concurrently, preserve failures, and aggregate exactly once."""

    def __init__(
        self,
        store: MissionRuntimeStore,
        organization_id: UUID,
        engine_factory: EngineFactory,
    ) -> None:
        self._store = store
        self._organization_id = organization_id
        self._engine_factory = engine_factory

    async def run(
        self, job: MissionRuntimeJob, cancel_event: asyncio.Event | None = None
    ) -> ParentRecommendation:
        if job.opportunity_id is None:
            raise ValueError("opportunity_next_action requires a server-bound opportunity")
        plan = MissionPlan.opportunity_next_action(
            self._organization_id,
            job.mission_id,
            supply_linked=job.case_id is not None and job.part_number is not None,
        )
        results: list[SpecialistResult] = []
        reviews: list[IndependentReview] = []
        persisted_ids: list[UUID] = []
        for stage in plan.stages:
            outcomes = await asyncio.gather(
                *(self._run_role(job, role, tuple(persisted_ids), cancel_event) for role in stage),
                return_exceptions=True,
            )
            for role, outcome in zip(stage, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    await self._store.persist_specialist_failure(
                        job, role, type(outcome).__name__
                    )
                    continue
                result, persisted_id, review = outcome
                results.append(result)
                persisted_ids.append(persisted_id)
                reviews.append(review)
        summary = (
            "; ".join(claim.conclusion for result in results for claim in result.claims)
            or "无法确认：没有可用的 Specialist 结果。"
        )
        parent = ParentAgent().aggregate(plan, tuple(results), tuple(reviews), summary[:4_000])
        await self._store.persist_parent_recommendation(job, parent)
        return parent

    async def _run_role(
        self,
        job: MissionRuntimeJob,
        role: SpecialistRole,
        input_artifact_ids: tuple[UUID, ...],
        cancel_event: asyncio.Event | None,
    ) -> tuple[SpecialistResult, UUID, IndependentReview]:
        tools = await self._store.mission_tools(job, role, input_artifact_ids)
        assignment = SpecialistAssignment(
            self._organization_id,
            job.mission_id,
            role,
            json.dumps(
                {
                    "mission": "opportunity_next_action",
                    "role": role.value,
                    "opportunityRef": str(job.opportunity_id),
                },
                separators=(",", ":"),
            ),
            input_artifact_ids,
            tuple(tool.name for tool in tools),
        )
        result = await SpecialistRunner(
            self._engine_factory(job),
            tools,
            self._store.trace_sink(job),
            lambda citation: self._store.citation_valid(job, citation),
        ).run(assignment, cancel_event)
        persisted_id = await self._store.persist_specialist_result(job, result)
        citations = tuple(citation for claim in result.claims for citation in claim.citations)
        valid = bool(citations) and all(
            await asyncio.gather(
                *(self._store.citation_valid(job, citation) for citation in citations)
            )
        )
        review = IndependentReview(
            result.artifact_id,
            "deterministic-citation-reviewer",
            ReviewVerdict.APPROVED if valid else ReviewVerdict.REJECTED,
            () if valid else ("citation_revalidation_failed",),
        )
        await self._store.persist_review(job, review)
        return result, persisted_id, review
