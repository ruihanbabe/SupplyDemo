"""F35 SQL-owned job lifecycle and a Worker that only consumes AgentEnginePort."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from psycopg import AsyncConnection

from product_api.crm import PostgresCrmFactRepository
from product_api.harness import (
    AgentEnginePort,
    HarnessProtocolError,
    HarnessToolError,
    HarnessTurnError,
    ToolDefinition,
    TraceSink,
)
from product_api.knowledge import PostgresKnowledgeRepository
from product_api.mission_runtime import DurableMissionRunner
from product_api.specialist_runner import (
    SpecialistResultError,
    SpecialistRunner,
    validate_knowledge_search,
    validate_no_arguments,
)
from product_api.specialists import (
    Citation,
    IndependentReview,
    ParentRecommendation,
    SpecialistAssignment,
    SpecialistResult,
    SpecialistRole,
)
from product_api.supply import PostgresSupplyRepository, calculate_impact
from product_api.tenant import TenantRepository
from product_api.trace import PostgresTraceRepository


class RuntimeStateError(Exception):
    """The SQL lease or persisted run context no longer permits this Worker action."""


@dataclass(frozen=True)
class WorkerIdentity:
    organization_id: UUID
    actor_id: UUID
    owner: str


@dataclass(frozen=True)
class WorkerPolicy:
    lease_seconds: int
    heartbeat_seconds: float
    max_attempts: int
    retry_seconds: int

    def __post_init__(self) -> None:
        if (
            not 1 <= self.lease_seconds <= 3600
            or not 0 < self.heartbeat_seconds < self.lease_seconds
        ):
            raise ValueError("heartbeat must be shorter than the valid lease")
        if not 1 <= self.max_attempts <= 20 or not 0 <= self.retry_seconds <= 86400:
            raise ValueError("invalid Worker retry policy")


@dataclass(frozen=True)
class RuntimeJob:
    id: UUID
    mission_id: UUID
    run_id: UUID
    trace_run_id: UUID
    case_id: UUID | None
    part_number: str | None
    model_policy_id: UUID | None
    generation: int
    checkpoint_sequence: int
    opportunity_id: UUID | None = None


class RuntimeStore(Protocol):
    async def reap(self) -> int: ...
    async def claim(self, lease_seconds: int) -> RuntimeJob | None: ...
    async def heartbeat(self, job: RuntimeJob, lease_seconds: int) -> bool: ...
    async def complete(self, job: RuntimeJob) -> bool: ...
    async def fail(self, job: RuntimeJob, code: str, policy: WorkerPolicy) -> bool: ...
    async def status(self, job: RuntimeJob) -> str: ...
    async def supply_impact(
        self, job: RuntimeJob, arguments: dict[str, object]
    ) -> dict[str, object]: ...
    async def crm_opportunity_facts(
        self, job: RuntimeJob, arguments: dict[str, object]
    ) -> dict[str, object]: ...
    async def citation_valid(self, job: RuntimeJob, citation: Citation) -> bool: ...
    async def persist_specialist_result(
        self, job: RuntimeJob, result: SpecialistResult
    ) -> UUID: ...
    def trace_sink(self, job: RuntimeJob) -> TraceSink: ...


class DurableTraceSink:
    """Each event commits independently, so a killed turn retains its audit trail."""

    def __init__(
        self,
        tenants: TenantRepository,
        identity: WorkerIdentity,
        traces: PostgresTraceRepository,
        job: RuntimeJob,
        write_lock: asyncio.Lock,
    ) -> None:
        self._tenants = tenants
        self._identity = identity
        self._traces = traces
        self._job = job
        self._write_lock = write_lock

    async def record(self, event_type: str, metadata: dict[str, object], sequence: int) -> None:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> None:
            checkpoint = {
                "protocolVersion": 1,
                "sequence": sequence,
                "eventType": event_type,
                "attempt": self._job.generation,
                "callId": metadata.get("callId"),
            }
            artifact_id = await self._traces.append_artifact(
                connection,
                self._job.trace_run_id,
                self._job.case_id,
                uuid4(),
                "pi_checkpoint",
                f"Worker checkpoint {self._job.generation}/{sequence}",
                checkpoint,
            )
            await self._traces.append_event(
                connection,
                self._job.trace_run_id,
                self._job.case_id,
                event_type,
                f"Worker event {event_type}",
                metadata,
                artifact_id,
            )

        async with self._write_lock:
            await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )


class PostgresRuntimeStore:
    def __init__(self, tenants: TenantRepository, identity: WorkerIdentity) -> None:
        self._tenants = tenants
        self._identity = identity
        self._traces = PostgresTraceRepository()
        self._supply = PostgresSupplyRepository()
        self._crm = PostgresCrmFactRepository()
        self._knowledge = PostgresKnowledgeRepository()
        self._trace_write_lock = asyncio.Lock()

    async def begin_supply(
        self,
        case_id: UUID,
        part_number: str,
        key: str,
        model_policy_id: UUID | None = None,
    ) -> tuple[UUID, UUID, UUID]:
        async def operation(
            connection: AsyncConnection[dict[str, object]],
        ) -> tuple[UUID, UUID, UUID]:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select * from app.begin_supply_runtime_run(%s,%s,%s,%s)",
                    (case_id, part_number, key, model_policy_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeStateError("run creation returned no IDs")
                return row["mission_id"], row["run_id"], row["job_id"]

        return await self._tenants.in_organization(
            self._identity.actor_id,
            self._identity.organization_id,
            operation,
        )

    async def cancel(self, job_id: UUID) -> bool:
        return bool(await self._scalar("select app.cancel_runtime_job(%s) as value", (job_id,)))

    async def reap(self) -> int:
        return int(await self._scalar("select app.reap_runtime_jobs() as value", ()))

    async def claim(self, lease_seconds: int) -> RuntimeJob | None:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> RuntimeJob | None:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "select * from app.claim_runtime_job(%s,%s)",
                    (self._identity.owner, lease_seconds),
                )
                claimed = await cursor.fetchone()
                if claimed is None:
                    return None

                async def reject_claim() -> None:
                    await cursor.execute(
                        "select app.fail_runtime_job(%s,%s,%s,'protocol_failed',1,0)",
                        (claimed["id"], self._identity.owner, claimed["attempts"]),
                    )

                if claimed["kind"] not in {"agent_turn", "opportunity_next_action"}:
                    await reject_claim()
                    return None
                await cursor.execute(
                    """select r.trace_run_id, m.id as mission_id, m.context_ref
                       from app.runtime_run r join app.runtime_mission m on m.id=r.mission_id
                       where r.id=%s""",
                    (claimed["run_id"],),
                )
                row = await cursor.fetchone()
                if row is None or row["trace_run_id"] is None:
                    await reject_claim()
                    return None
                refs = row["context_ref"]
                expected = {"caseId", "partNumber", "modelPolicyId"}
                if claimed["kind"] == "opportunity_next_action":
                    expected.add("opportunityId")
                if not isinstance(refs, dict) or set(refs) != expected:
                    await reject_claim()
                    return None
                try:
                    case_id = UUID(str(refs["caseId"])) if refs["caseId"] else None
                    policy_id = UUID(str(refs["modelPolicyId"])) if refs["modelPolicyId"] else None
                    opportunity_id = (
                        UUID(str(refs["opportunityId"]))
                        if claimed["kind"] == "opportunity_next_action"
                        else None
                    )
                except ValueError:
                    await reject_claim()
                    return None
                part_number = refs["partNumber"]
                if part_number is not None and (
                    not isinstance(part_number, str) or not 1 <= len(part_number) <= 160
                ):
                    await reject_claim()
                    return None
                if claimed["kind"] == "agent_turn" and (case_id is None or part_number is None):
                    await reject_claim()
                    return None
                if case_id is not None:
                    await cursor.execute(
                        "select 1 from app.supply_case where id=%s and scope->'partNumbers' ? %s",
                        (case_id, part_number),
                    )
                    if await cursor.fetchone() is None:
                        await reject_claim()
                        return None
                if opportunity_id is not None:
                    await cursor.execute(
                        "select 1 from app.crm_opportunity where id=%s and supply_case_id is not distinct from %s",
                        (opportunity_id, case_id),
                    )
                    if await cursor.fetchone() is None:
                        await reject_claim()
                        return None
                if not await self._traces.verify_chain(connection, row["trace_run_id"]):
                    await reject_claim()
                    return None
                artifacts = await self._traces.list_artifacts(connection, row["trace_run_id"])
                checkpoints = [
                    item
                    for item in artifacts
                    if item.artifact_type == "pi_checkpoint"
                    and item.redacted_payload.get("protocolVersion") == 1
                ]
                last = max(checkpoints, key=lambda item: item.created_at) if checkpoints else None
                sequence = int(last.redacted_payload["sequence"]) if last else 0
                return RuntimeJob(
                    claimed["id"],
                    row["mission_id"],
                    claimed["run_id"],
                    row["trace_run_id"],
                    case_id,
                    part_number,
                    policy_id,
                    claimed["attempts"],
                    sequence,
                    opportunity_id,
                )

        return await self._tenants.in_organization(
            self._identity.actor_id,
            self._identity.organization_id,
            operation,
        )

    async def heartbeat(self, job: RuntimeJob, lease_seconds: int) -> bool:
        return bool(
            await self._scalar(
                "select app.heartbeat_runtime_job(%s,%s,%s,%s) as value",
                (job.id, self._identity.owner, job.generation, lease_seconds),
            )
        )

    async def complete(self, job: RuntimeJob) -> bool:
        return bool(
            await self._scalar(
                "select app.complete_runtime_job(%s,%s,%s) as value",
                (job.id, self._identity.owner, job.generation),
            )
        )

    async def fail(self, job: RuntimeJob, code: str, policy: WorkerPolicy) -> bool:
        return bool(
            await self._scalar(
                "select app.fail_runtime_job(%s,%s,%s,%s,%s,%s) as value",
                (
                    job.id,
                    self._identity.owner,
                    job.generation,
                    code,
                    policy.max_attempts,
                    policy.retry_seconds,
                ),
            )
        )

    async def status(self, job: RuntimeJob) -> str:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> str:
            async with connection.cursor() as cursor:
                await cursor.execute("select status from app.runtime_job where id=%s", (job.id,))
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeStateError("job no longer visible")
                return str(row["status"])

        return await self._tenants.in_organization(
            self._identity.actor_id,
            self._identity.organization_id,
            operation,
        )

    async def supply_impact(
        self, job: RuntimeJob, arguments: dict[str, object]
    ) -> dict[str, object]:
        if job.part_number is None or job.case_id is None:
            raise HarnessToolError("job 没有服务端绑定的 supply scope")
        if arguments["partNumber"] != job.part_number:
            raise HarnessToolError("tool part is outside the job's authorized case")
        as_of = datetime.now(UTC).date()
        if "asOf" in arguments and arguments["asOf"] != as_of.isoformat():
            raise HarnessToolError("tool date differs from current verified facts")

        async def operation(connection: AsyncConnection[dict[str, object]]) -> dict[str, object]:
            facts = await self._supply.load_facts(connection, job.part_number)
            impact = calculate_impact(facts, job.part_number, as_of)
            payload: dict[str, object] = {
                "partNumber": job.part_number,
                "asOf": as_of.isoformat(),
                "projectedAvailableQty": _decimal_text(impact.projected_available_qty),
                "firstShortageOn": impact.first_shortage_on.isoformat()
                if impact.first_shortage_on
                else None,
                "unknowns": impact.unknowns,
            }
            artifact_id = await self._traces.append_artifact(
                connection,
                job.trace_run_id,
                job.case_id,
                uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:supply-impact"),
                "tool_result",
                f"Current supply impact for {job.part_number}",
                payload,
            )
            artifact = await self._traces.get_artifact(connection, artifact_id)
            if artifact is None:
                raise RuntimeStateError("persisted tool result is not readable")
            await self._traces.append_event(
                connection,
                job.trace_run_id,
                job.case_id,
                "tool.result_recorded",
                "Read-only supply result recorded",
                {"tool": "supply.impact", "artifactVersion": artifact.version},
                artifact.id,
            )
            return {
                **payload,
                "citation": {
                    "sourceKind": "trace_artifact",
                    "sourceId": str(artifact.id),
                    "version": str(artifact.version),
                    "locator": "redacted_payload",
                    "fragmentHash": artifact.content_hash,
                },
            }

        async with self._trace_write_lock:
            return await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )

    async def knowledge_search(
        self, job: RuntimeJob, arguments: dict[str, object]
    ) -> dict[str, object]:
        validate_knowledge_search(arguments)

        async def operation(connection: AsyncConnection[dict[str, object]]) -> dict[str, object]:
            hits = await self._knowledge.search(
                connection, str(arguments["query"]), int(arguments.get("limit", 5))
            )
            payload: dict[str, object] = {
                "query": str(arguments["query"]),
                "hits": [
                    {
                        "documentId": str(hit.document_id),
                        "title": hit.document_title,
                        "kind": hit.document_kind.value,
                        "body": hit.body,
                        "citation": {
                            "sourceKind": hit.citation().source_kind,
                            "sourceId": hit.citation().source_id,
                            "version": hit.citation().version,
                            "locator": hit.citation().locator,
                            "fragmentHash": hit.citation().fragment_hash,
                        },
                    }
                    for hit in hits
                ],
            }
            return await self._record_tool_result(
                connection,
                job,
                "knowledge.search",
                payload,
                uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:knowledge:{arguments['query']}"),
            )

        async with self._trace_write_lock:
            return await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )

    async def mission_claims(
        self,
        job: RuntimeJob,
        input_artifact_ids: tuple[UUID, ...],
        arguments: dict[str, object],
    ) -> dict[str, object]:
        validate_no_arguments(arguments)

        async def operation(connection: AsyncConnection[dict[str, object]]) -> dict[str, object]:
            artifacts = []
            for artifact_id in input_artifact_ids:
                artifact = await self._traces.get_artifact(connection, artifact_id)
                if (
                    artifact is None
                    or artifact.run_id != job.trace_run_id
                    or artifact.artifact_type != "specialist_result"
                ):
                    raise HarnessToolError("input artifact is outside the Mission scope")
                artifacts.append(
                    {
                        "artifactId": str(artifact.id),
                        "role": artifact.redacted_payload.get("role"),
                        "claims": artifact.redacted_payload.get("claims", []),
                    }
                )
            return await self._record_tool_result(
                connection,
                job,
                "mission.claims",
                {"results": artifacts},
                uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:mission-claims"),
            )

        async with self._trace_write_lock:
            return await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )

    async def mission_tools(
        self,
        job: RuntimeJob,
        role: SpecialistRole,
        input_artifact_ids: tuple[UUID, ...],
    ) -> tuple[ToolDefinition, ...]:
        if role is SpecialistRole.CUSTOMER_FACTS:
            return (
                ToolDefinition(
                    "crm.opportunity_facts",
                    lambda arguments: self.crm_opportunity_facts(job, arguments),
                    validate_no_arguments,
                ),
            )
        if role is SpecialistRole.BUSINESS_POLICY:
            return (
                ToolDefinition(
                    "knowledge.search",
                    lambda arguments: self.knowledge_search(job, arguments),
                    validate_knowledge_search,
                ),
            )
        if role is SpecialistRole.SUPPLY_RISK:
            return (
                ToolDefinition(
                    "supply.impact",
                    lambda arguments: self.supply_impact(job, arguments),
                ),
            )
        if role in {SpecialistRole.OPPORTUNITY_DIAGNOSIS, SpecialistRole.PROCUREMENT_STRATEGY}:
            return (
                ToolDefinition(
                    "mission.claims",
                    lambda arguments: self.mission_claims(job, input_artifact_ids, arguments),
                    validate_no_arguments,
                ),
            )
        raise RuntimeStateError("unsupported Specialist role")

    async def _record_tool_result(
        self,
        connection: AsyncConnection[dict[str, object]],
        job: RuntimeJob,
        tool_name: str,
        payload: dict[str, object],
        artifact_key: UUID,
    ) -> dict[str, object]:
        artifact_id = await self._traces.append_artifact(
            connection,
            job.trace_run_id,
            job.case_id,
            artifact_key,
            "tool_result",
            f"{tool_name} result recorded",
            payload,
        )
        artifact = await self._traces.get_artifact(connection, artifact_id)
        if artifact is None:
            raise RuntimeStateError("persisted tool result is not readable")
        await self._traces.append_event(
            connection,
            job.trace_run_id,
            job.case_id,
            "tool.result_recorded",
            "Read-only tool result recorded",
            {"tool": tool_name, "artifactVersion": artifact.version},
            artifact.id,
        )
        return {
            **payload,
            "citation": {
                "sourceKind": "trace_artifact",
                "sourceId": str(artifact.id),
                "version": str(artifact.version),
                "locator": "redacted_payload",
                "fragmentHash": artifact.content_hash,
            },
        }

    async def crm_opportunity_facts(
        self, job: RuntimeJob, arguments: dict[str, object]
    ) -> dict[str, object]:
        validate_no_arguments(arguments)
        if job.opportunity_id is None:
            raise HarnessToolError("job 没有服务端绑定的 opportunity scope")

        async def operation(connection: AsyncConnection[dict[str, object]]) -> dict[str, object]:
            facts = await self._crm.load_opportunity_facts(connection, job.opportunity_id)
            payload: dict[str, object] = {
                "customer": {
                    "id": str(facts.customer.id),
                    "displayName": facts.customer.display_name,
                    "lifecycle": facts.customer.lifecycle,
                    "version": facts.customer.version,
                },
                "opportunity": {
                    "id": str(facts.opportunity.id),
                    "title": facts.opportunity.title,
                    "stage": facts.opportunity.stage,
                    "supplyLinked": facts.opportunity.supply_case_id is not None,
                    "version": facts.opportunity.version,
                },
                "contacts": [
                    {"id": str(item.id), "displayName": item.display_name, "version": item.version}
                    for item in facts.contacts
                ],
                "conversations": [
                    {
                        "id": str(item.id),
                        "channel": item.channel,
                        "summary": item.summary,
                        "version": item.version,
                        "updatedAt": item.updated_at.isoformat(),
                    }
                    for item in facts.conversations
                ],
                "tasks": [
                    {
                        "id": str(item.id),
                        "title": item.title,
                        "status": item.status,
                        "version": item.version,
                    }
                    for item in facts.tasks
                ],
            }
            artifact_id = await self._traces.append_artifact(
                connection,
                job.trace_run_id,
                job.case_id,
                uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:crm-opportunity-facts"),
                "tool_result",
                "Scoped CRM opportunity facts recorded",
                payload,
            )
            artifact = await self._traces.get_artifact(connection, artifact_id)
            if artifact is None:
                raise RuntimeStateError("persisted CRM tool result is not readable")
            await self._traces.append_event(
                connection,
                job.trace_run_id,
                job.case_id,
                "tool.result_recorded",
                "Read-only CRM result recorded",
                {"tool": "crm.opportunity_facts", "artifactVersion": artifact.version},
                artifact.id,
            )
            return {
                **payload,
                "citation": {
                    "sourceKind": "trace_artifact",
                    "sourceId": str(artifact.id),
                    "version": str(artifact.version),
                    "locator": "redacted_payload",
                    "fragmentHash": artifact.content_hash,
                },
            }

        async with self._trace_write_lock:
            return await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )

    async def citation_valid(self, job: RuntimeJob, citation: Citation) -> bool:
        if citation.source_kind != "trace_artifact" or citation.locator != "redacted_payload":
            return False
        try:
            artifact_id = UUID(citation.source_id)
            version = int(citation.version)
        except (TypeError, ValueError):
            return False

        async def operation(connection: AsyncConnection[dict[str, object]]) -> bool:
            artifact = await self._traces.get_artifact(connection, artifact_id)
            return bool(
                artifact
                and artifact.run_id == job.trace_run_id
                and artifact.case_id == job.case_id
                and artifact.artifact_type == "tool_result"
                and artifact.version == version
                and artifact.content_hash == citation.fragment_hash
            )

        return bool(
            await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )
        )

    async def persist_specialist_result(self, job: RuntimeJob, result: SpecialistResult) -> UUID:
        if (
            result.organization_id != self._identity.organization_id
            or result.mission_id != job.mission_id
        ):
            raise RuntimeStateError("specialist result is outside the claimed job scope")

        async def operation(connection: AsyncConnection[dict[str, object]]) -> UUID:
            artifact_id = await self._traces.append_artifact(
                connection,
                job.trace_run_id,
                job.case_id,
                result.artifact_id,
                "specialist_result",
                f"{result.role.value} returned {len(result.claims)} claims",
                _specialist_payload(result),
            )
            await self._traces.append_event(
                connection,
                job.trace_run_id,
                job.case_id,
                "specialist.result_saved",
                "Validated specialist result saved",
                {
                    "role": result.role.value,
                    "complete": result.complete,
                    "claimCount": len(result.claims),
                },
                artifact_id,
            )
            return artifact_id

        async with self._trace_write_lock:
            return await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )

    async def persist_review(self, job: RuntimeJob, review: IndependentReview) -> UUID:
        return await self._persist_mission_artifact(
            job,
            uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:review:{review.target_artifact_id}"),
            "specialist_review",
            "Independent citation review saved",
            {
                "targetArtifactKey": str(review.target_artifact_id),
                "reviewer": review.reviewer,
                "verdict": review.verdict.value,
                "issues": list(review.issues),
            },
        )

    async def persist_specialist_failure(
        self, job: RuntimeJob, role: SpecialistRole, code: str
    ) -> UUID:
        return await self._persist_mission_artifact(
            job,
            uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:failure:{role.value}"),
            "specialist_failure",
            f"{role.value} failed",
            {"role": role.value, "code": code},
        )

    async def persist_parent_recommendation(
        self, job: RuntimeJob, recommendation: ParentRecommendation
    ) -> UUID:
        payload: dict[str, object] = {
            "missionId": str(recommendation.mission_id),
            "quality": recommendation.quality.value,
            "recommendation": recommendation.recommendation,
            "missingRoles": [role.value for role in recommendation.missing_roles],
            "issues": list(recommendation.issues),
            "claimIds": [str(claim.claim_id) for claim in recommendation.claims],
            "proposalDraft": None,
        }
        if recommendation.proposal_draft is not None:
            payload["proposalDraft"] = {
                "executable": False,
                "sourceArtifactKeys": [
                    str(item) for item in recommendation.proposal_draft.source_artifact_ids
                ],
                "claimIds": [str(item) for item in recommendation.proposal_draft.claim_ids],
            }
        return await self._persist_mission_artifact(
            job,
            uuid5(NAMESPACE_URL, f"supplyagent:{job.id}:parent-recommendation"),
            "parent_recommendation",
            "Serial parent recommendation saved",
            payload,
        )

    async def _persist_mission_artifact(
        self,
        job: RuntimeJob,
        artifact_key: UUID,
        artifact_type: str,
        summary: str,
        payload: dict[str, object],
    ) -> UUID:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> UUID:
            artifact_id = await self._traces.append_artifact(
                connection,
                job.trace_run_id,
                job.case_id,
                artifact_key,
                artifact_type,
                summary,
                payload,
            )
            await self._traces.append_event(
                connection,
                job.trace_run_id,
                job.case_id,
                f"{artifact_type}.saved",
                summary,
                {"artifactType": artifact_type},
                artifact_id,
            )
            return artifact_id

        async with self._trace_write_lock:
            return await self._tenants.in_organization(
                self._identity.actor_id,
                self._identity.organization_id,
                operation,
            )

    def trace_sink(self, job: RuntimeJob) -> TraceSink:
        return DurableTraceSink(
            self._tenants, self._identity, self._traces, job, self._trace_write_lock
        )

    async def _scalar(self, query: str, params: tuple[object, ...]) -> object:
        async def operation(connection: AsyncConnection[dict[str, object]]) -> object:
            async with connection.cursor() as cursor:
                await cursor.execute(query, params)
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeStateError("runtime command returned no result")
                return row["value"]

        return await self._tenants.in_organization(
            self._identity.actor_id,
            self._identity.organization_id,
            operation,
        )


EngineFactory = Callable[[RuntimeJob], AgentEnginePort]


class DurableWorker:
    def __init__(
        self,
        store: RuntimeStore,
        identity: WorkerIdentity,
        policy: WorkerPolicy,
        engine_factory: EngineFactory,
    ) -> None:
        self._store = store
        self._identity = identity
        self._policy = policy
        self._engine_factory = engine_factory

    async def run_once(self) -> bool:
        await self._store.reap()
        job = await self._store.claim(self._policy.lease_seconds)
        if job is None:
            return False
        cancel = asyncio.Event()
        heartbeat = asyncio.create_task(self._keep_lease(job, cancel))
        try:
            if job.opportunity_id is not None:
                await DurableMissionRunner(
                    self._store, self._identity.organization_id, self._engine_factory
                ).run(job, cancel)
                if not await self._store.complete(job):
                    raise RuntimeStateError("job lease lost before completion")
                return True
            task = "Investigate the current supply impact for this authorized part: " + json.dumps(
                {"partNumber": job.part_number},
                ensure_ascii=False,
            )
            assignment = SpecialistAssignment(
                self._identity.organization_id,
                job.mission_id,
                SpecialistRole.SUPPLY_RISK,
                task,
                (job.case_id,),
                ("supply.impact",),
            )
            tools = (
                ToolDefinition(
                    "supply.impact",
                    lambda arguments: self._store.supply_impact(job, arguments),
                ),
            )
            result = await SpecialistRunner(
                self._engine_factory(job),
                tools,
                self._store.trace_sink(job),
                lambda citation: self._store.citation_valid(job, citation),
            ).run(assignment, cancel)
            await self._store.persist_specialist_result(job, result)
            if not await self._store.complete(job):
                raise RuntimeStateError("job lease lost before completion")
        except HarnessTurnError as error:
            if not await self._store.fail(job, error.code, self._policy):
                raise RuntimeStateError("job lease lost after turn failure") from error
        except (HarnessProtocolError, HarnessToolError, SpecialistResultError) as error:
            if not await self._store.fail(job, "protocol_failed", self._policy):
                raise RuntimeStateError("job lease lost after protocol failure") from error
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def _keep_lease(self, job: RuntimeJob, cancel: asyncio.Event) -> None:
        while not cancel.is_set():
            await asyncio.sleep(self._policy.heartbeat_seconds)
            try:
                renewed = await self._store.heartbeat(job, self._policy.lease_seconds)
            except (OSError, RuntimeError):
                renewed = False
            if not renewed:
                cancel.set()
                return


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _specialist_payload(result: SpecialistResult) -> dict[str, object]:
    return {
        "artifactId": str(result.artifact_id),
        "missionId": str(result.mission_id),
        "role": result.role.value,
        "complete": result.complete,
        "claims": [
            {
                "claimId": str(claim.claim_id),
                "subject": claim.subject,
                "claimType": claim.claim_type,
                "conclusion": claim.conclusion,
                "quality": claim.quality.value,
                "citations": [
                    {
                        "sourceKind": citation.source_kind,
                        "sourceId": citation.source_id,
                        "version": citation.version,
                        "locator": citation.locator,
                        "fragmentHash": citation.fragment_hash,
                    }
                    for citation in claim.citations
                ],
                "unknowns": list(claim.unknowns),
                "conflicts": list(claim.conflicts),
            }
            for claim in result.claims
        ],
    }
