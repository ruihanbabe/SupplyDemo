"""F35 Worker lease, retry, cancellation, and engine-port tests without services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from product_api.harness import (
    HarnessContext,
    HarnessTurnError,
    MemoryTraceSink,
    ToolRegistry,
    TurnResult,
)
from product_api.runtime import DurableWorker, RuntimeJob, WorkerIdentity, WorkerPolicy
from product_api.specialists import Citation, SpecialistResult


@dataclass
class FakeStore:
    job: RuntimeJob | None
    state: str = "queued"
    events: MemoryTraceSink = field(default_factory=MemoryTraceSink)
    failures: list[str] = field(default_factory=list)
    reaps: int = 0
    impacts: int = 0
    persisted: list[SpecialistResult] = field(default_factory=list)

    async def reap(self) -> int:
        self.reaps += 1
        return 0

    async def claim(self, lease_seconds: int) -> RuntimeJob | None:
        assert lease_seconds == 2
        job, self.job = self.job, None
        if job:
            self.state = "leased"
        return job

    async def heartbeat(self, job: RuntimeJob, lease_seconds: int) -> bool:
        assert lease_seconds == 2
        return self.state == "leased"

    async def complete(self, job: RuntimeJob) -> bool:
        if self.state != "leased":
            return False
        self.state = "completed"
        return True

    async def fail(self, job: RuntimeJob, code: str, policy: WorkerPolicy) -> bool:
        if self.state != "leased":
            return False
        self.failures.append(code)
        self.state = "failed"
        return True

    async def status(self, job: RuntimeJob) -> str:
        return self.state

    async def supply_impact(self, job: RuntimeJob, arguments: dict[str, object]) -> dict[str, object]:
        self.impacts += 1
        assert arguments == {"partNumber": job.part_number}
        return {"partNumber": job.part_number, "unknowns": []}

    async def citation_valid(self, job: RuntimeJob, citation: Citation) -> bool:
        return citation.source_kind == "trace_artifact"

    async def persist_specialist_result(
        self, job: RuntimeJob, result: SpecialistResult
    ) -> object:
        self.persisted.append(result)
        return result.artifact_id

    def trace_sink(self, job: RuntimeJob) -> MemoryTraceSink:
        return self.events


class PortEngine:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure

    async def run_turn(
        self, context: HarnessContext, tools: ToolRegistry,
        trace: MemoryTraceSink, cancel_event: asyncio.Event | None = None,
    ) -> TurnResult:
        assert context.fact_references[0].startswith("artifact:")
        await trace.record("pi.turn_started", {"engine": "fake-port"}, 1)
        if self.failure:
            raise HarnessTurnError(self.failure)
        await tools.execute("supply.impact", {"partNumber": "PART-A"})
        await trace.record("pi.turn_completed", {}, 2)
        return TurnResult(
            uuid4(),
            "completed",
            ("supply.impact",),
            2,
            {
                "role": "supply_risk",
                "complete": True,
                "claims": [
                    {
                        "subject": "PART-A",
                        "claimType": "supply_impact",
                        "conclusion": "PART-A impact read completed.",
                        "quality": "supported",
                        "citations": [
                            {
                                "sourceKind": "trace_artifact",
                                "sourceId": str(uuid4()),
                                "version": "1",
                                "locator": "redacted_payload",
                                "fragmentHash": "a" * 64,
                            }
                        ],
                        "unknowns": [],
                        "conflicts": [],
                    }
                ],
            },
        )


def _worker(failure: str | None = None) -> tuple[DurableWorker, FakeStore]:
    identity = WorkerIdentity(uuid4(), uuid4(), "worker-1")
    job = RuntimeJob(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "PART-A", None, 2, 3
    )
    store = FakeStore(job)
    worker = DurableWorker(store, identity, WorkerPolicy(2, 0.1, 2, 0), lambda _: PortEngine(failure))
    return worker, store


def test_worker_consumes_engine_port_and_completes() -> None:
    async def scenario() -> None:
        worker, store = _worker()
        assert await worker.run_once()
        assert store.state == "completed" and store.impacts == 1
        assert len(store.persisted) == 1
        assert store.persisted[0].role.value == "supply_risk"
        assert [event["eventType"] for event in store.events.events] == [
            "pi.turn_started", "pi.turn_completed",
        ]
        assert not await worker.run_once() and store.reaps == 2

    asyncio.run(scenario())


def test_worker_maps_engine_failure_to_durable_retry_command() -> None:
    async def scenario() -> None:
        worker, store = _worker("model_failed")
        assert await worker.run_once()
        assert store.failures == ["model_failed"]

    asyncio.run(scenario())
