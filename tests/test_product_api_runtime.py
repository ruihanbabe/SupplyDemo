"""F35 Worker lease, retry, cancellation, and engine-port tests without services."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from product_api import worker_main
from product_api.harness import (
    HarnessContext,
    HarnessTurnError,
    MemoryTraceSink,
    ToolRegistry,
    TurnResult,
)
from product_api.runtime import (
    DurableWorker,
    RejectedClaim,
    RuntimeJob,
    WorkerIdentity,
    WorkerPolicy,
    WorkerRunReport,
)
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
    checkpoints: list = field(default_factory=list)

    async def checkpoint(self, job, phase, detail):
        if self.state != "leased":
            return False
        self.checkpoints.append((phase, detail))
        return True

    async def reap(self) -> int:
        self.reaps += 1
        return 0

    async def claim_next(self, lease_seconds: int) -> RuntimeJob | None:
        assert lease_seconds == 2
        job, self.job = self.job, None
        if job:
            self.state = "leased"
        return job

    async def claim(self, lease_seconds: int) -> RuntimeJob | None:
        return await self.claim_next(lease_seconds)

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
        self.state = "failed" if job.generation >= policy.max_attempts else "queued"
        return True

    async def fail_outcome(self, job: RuntimeJob, code: str, policy: WorkerPolicy):
        if self.state != "leased":
            return None
        await self.fail(job, code, policy)
        if code == "cancelled":
            self.state = "cancelled"
        return self.state

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


@pytest.mark.parametrize("failure,attempts,outcome,exit_code", [
    (None, 2, "completed", 0),
    ("model_failed", 2, "failed", 1),
    ("model_failed", 3, "retry_scheduled", 1),
    ("cancelled", 2, "cancelled", 0),
])
def test_worker_report_uses_actual_transition(failure, attempts, outcome, exit_code):
    async def scenario():
        worker, store = _worker(failure)
        worker._policy = WorkerPolicy(2, .1, attempts, 0)
        report = await worker.run_once_report()
        assert report.outcome == outcome and report.exit_code == exit_code
        assert store.state == ("queued" if outcome == "retry_scheduled" else outcome)
        payload = report.payload()
        assert payload["schemaVersion"] == 1 and payload["jobId"] and payload["generation"] == 2
        assert payload["traceRunId"] != payload["runId"]
        idle = await worker.run_once_report()
        assert idle.outcome == "idle" and idle.job_id is None and idle.exit_code == 0

    asyncio.run(scenario())


def test_worker_report_distinguishes_rejected_claim_and_lost_lease():
    async def scenario():
        worker, store = _worker()
        job = store.job
        store.job = RejectedClaim(job.id, job.generation, job.run_id)
        rejected = await worker.run_once_report()
        assert rejected.outcome == "claim_rejected" and rejected.job_id == job.id
        assert rejected.exit_code == 1
        worker, store = _worker()

        async def lose_lease(job):
            store.state = "queued"
            return False

        store.complete = lose_lease
        report = await worker.run_once_report()
        assert report.outcome == "lease_lost" and store.state != "completed"

    asyncio.run(scenario())


def test_worker_stop_cancels_active_turn_and_does_not_claim_again():
    async def scenario():
        worker, store = _worker()
        stop, started = asyncio.Event(), asyncio.Event()

        class WaitingEngine:
            async def run_turn(self, context, tools, trace, cancel_event):
                started.set()
                await cancel_event.wait()
                raise HarnessTurnError("cancelled")

        worker._engine_factory = lambda _: WaitingEngine()
        task = asyncio.create_task(worker.run_once_report(stop))
        async with asyncio.timeout(2):
            await started.wait()
            stop.set()
            report = await task
        assert report.outcome == "cancelled" and store.state == "cancelled"
        reaps = store.reaps
        assert (await worker.run_once_report(stop)).outcome == "cancelled"
        assert store.reaps == reaps

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,outcome,code", [
    (None, "completed", 0), ("model_failed", "failed", 1),
])
def test_worker_cli_once_prints_versioned_report(monkeypatch, capsys, failure, outcome, code):
    worker, _ = _worker(failure)
    monkeypatch.setattr(worker_main, "create_worker", lambda: worker)
    assert asyncio.run(worker_main.run_worker(
        once=True, poll_seconds=2, stop=asyncio.Event(),
    )) == code
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == outcome and payload["schemaVersion"] == 1


def test_worker_instances_and_startup_errors_are_safe(monkeypatch, capsys):
    monkeypatch.setenv("SUPPLYAGENT_WORKER_OWNER", "test-deployment")
    monkeypatch.setenv("SUPPLYAGENT_WORKER_ORGANIZATION_ID", str(uuid4()))
    monkeypatch.setenv("SUPPLYAGENT_WORKER_ACTOR_ID", str(uuid4()))
    monkeypatch.setenv("SUPPLYAGENT_PRODUCT_DATABASE_URL", "synthetic-no-connection")
    first, second = worker_main.create_worker(), worker_main.create_worker()
    assert first.report("idle").worker_instance_id != second.report("idle").worker_instance_id
    monkeypatch.setattr(worker_main, "load_dotenv", lambda _: None)
    monkeypatch.setattr("sys.argv", ["worker", "--once"])
    monkeypatch.setenv("SUPPLYAGENT_WORKER_ACTOR_ID", "SECRET_INVALID_ACTOR")
    assert worker_main.main() == 2
    output = capsys.readouterr().out
    assert "SECRET_INVALID_ACTOR" not in output
    assert json.loads(output)["outcome"] == "process_error"
    assert WorkerRunReport(uuid4(), "test", "process_error").exit_code == 2



def test_report_keeps_job_identity_when_storage_fails_and_hides_exception():
    async def scenario():
        worker, store = _worker()
        job = store.job

        async def broken(*args):
            raise OSError("SECRET_DSN")

        store.complete = broken
        report = await worker.run_once_report()
        assert report.outcome == "process_error" and report.exit_code == 2
        assert report.job_id == job.id and store.state == "leased"
        assert "SECRET_DSN" not in json.dumps(report.payload())

    asyncio.run(scenario())


def test_cli_idle_stop_wakes_without_waiting_for_poll_interval(monkeypatch, capsys):
    async def scenario():
        worker, store = _worker()
        store.job = None
        stop = asyncio.Event()
        monkeypatch.setattr(worker_main, "create_worker", lambda: worker)
        original = worker.run_once_report

        async def idle_then_stop(event):
            result = await original(event)
            stop.set()
            return result

        worker.run_once_report = idle_then_stop
        async with asyncio.timeout(1):
            assert await worker_main.run_worker(once=False, poll_seconds=60, stop=stop) == 0
        assert json.loads(capsys.readouterr().out)["outcome"] == "idle"

    asyncio.run(scenario())
