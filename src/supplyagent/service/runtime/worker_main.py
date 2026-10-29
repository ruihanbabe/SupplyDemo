"""One-organization F35 Worker process; the database remains the queue and audit truth."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import signal
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dotenv import load_dotenv
from psycopg import Error as PostgresError

from supplyagent.paths import PROJECT_ROOT
from supplyagent.sdk.engine.harness import HarnessTurnError, PiRuntimeAdapter
from supplyagent.service.gateway.gateway import CapabilitySigner, ModelCapability
from supplyagent.service.persistence.tenant import PostgresTenantRepository, TenantAccessError
from supplyagent.service.runtime.runtime import (
    DurableWorker,
    PostgresRuntimeStore,
    RuntimeJob,
    WorkerIdentity,
    WorkerPolicy,
)

PROCESS_ERRORS = (
    OSError,
    ValueError,
    RuntimeError,
    PostgresError,
    TenantAccessError,
    ExceptionGroup,
)

SIDECAR = PROJECT_ROOT / "pi_sidecar" / "f42_protocol_sidecar.mjs"


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def create_worker() -> DurableWorker:
    owner = _required("SUPPLYAGENT_WORKER_OWNER")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,87}", owner):
        raise ValueError("Worker owner must be a short deployment label")
    identity = WorkerIdentity(
        UUID(_required("SUPPLYAGENT_WORKER_ORGANIZATION_ID")),
        UUID(_required("SUPPLYAGENT_WORKER_ACTOR_ID")),
        owner + "-" + uuid4().hex,
    )
    policy = WorkerPolicy(
        int(os.getenv("SUPPLYAGENT_WORKER_LEASE_SECONDS", "30")),
        float(os.getenv("SUPPLYAGENT_WORKER_HEARTBEAT_SECONDS", "10")),
        int(os.getenv("SUPPLYAGENT_WORKER_MAX_ATTEMPTS", "3")),
        int(os.getenv("SUPPLYAGENT_WORKER_RETRY_SECONDS", "30")),
    )
    tenants = PostgresTenantRepository(_required("SUPPLYAGENT_PRODUCT_DATABASE_URL"))
    gateway_url = os.getenv("SUPPLYAGENT_GATEWAY_URL")
    signing_key = os.getenv("SUPPLYAGENT_GATEWAY_SIGNING_KEY")
    signer = CapabilitySigner(signing_key.encode()) if signing_key else None
    replay = os.getenv("SUPPLYAGENT_WORKER_REPLAY") == "1"

    from supplyagent.sdk.context.compiler import ContextPolicy
    from supplyagent.service.context.session import ContextSession

    context_policy = ContextPolicy.load()
    from supplyagent.service.memory.worker_main import backend_factory, load_memory_policy

    memory_policy = load_memory_policy()
    if memory_policy["enabled"] and not context_policy.settings["enabled"]:
        raise ValueError("memory requires per-request Context")
    memory_backend = backend_factory() if memory_policy["enabled"] else None

    def engine(job: RuntimeJob) -> PiRuntimeAdapter:
        command = ("node", str(SIDECAR))
        context_session = (
            ContextSession(
                tenants, identity, job, context_policy, memory_backend_factory=memory_backend
            )
            if context_policy.settings["enabled"]
            else None
        )
        context_options = (
            {"protocol_version": 3, "context_session": context_session}
            if context_session
            else {"protocol_version": 2}
        )
        if replay and job.model_policy_id is None:
            return PiRuntimeAdapter(
                command, timeout_seconds=policy.lease_seconds * 2, **context_options
            )
        if job.model_policy_id is None or signer is None or not gateway_url:
            raise HarnessTurnError("model_failed")
        capability = signer.issue(
            ModelCapability(
                identity.actor_id,
                identity.organization_id,
                job.trace_run_id,
                job.model_policy_id,
                int(time.time()) + min(policy.lease_seconds * 10, 3600),
                job_id=job.id, generation=job.generation, owner=identity.owner,
            )
        )
        return PiRuntimeAdapter(
            command,
            timeout_seconds=policy.lease_seconds * 2,
            gateway_url=gateway_url,
            gateway_capability=capability,
            **context_options,
        )

    return DurableWorker(PostgresRuntimeStore(tenants, identity), identity, policy, engine)


async def run_worker(
    *,
    once: bool,
    poll_seconds: float,
    stop: asyncio.Event | None = None,
) -> int:
    if not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ValueError("poll interval must be positive")
    worker = create_worker()
    owned_signals = stop is None
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    if owned_signals:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            try:
                report = await worker.run_once_report(stop)
            except PROCESS_ERRORS:
                report = worker.report("process_error", error_code="worker_failed")
            print(json.dumps(report.payload(), separators=(",", ":")), flush=True)
            if once or report.outcome == "process_error":
                return report.exit_code
            if report.outcome == "idle":
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
        return 0
    finally:
        if owned_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


def main() -> int:
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Run one organization's durable F35 Worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    arguments = parser.parse_args()
    try:
        return asyncio.run(run_worker(once=arguments.once, poll_seconds=arguments.poll_seconds))
    except PROCESS_ERRORS:
        # Configuration and connection errors may contain credentials; never print their text.
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "outcome": "process_error",
                    "errorCode": "worker_start_failed",
                    "reportedAt": datetime.now(UTC).isoformat(),
                    "organizationId": None,
                    "workerInstanceId": None,
                    "jobId": None,
                    "generation": None,
                    "runId": None,
                    "traceRunId": None,
                }
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
