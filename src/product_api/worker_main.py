"""One-organization F35 Worker process; the database remains the queue and audit truth."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

from product_api.gateway import CapabilitySigner, ModelCapability
from product_api.harness import HarnessTurnError, PiRuntimeAdapter
from product_api.runtime import (
    DurableWorker,
    PostgresRuntimeStore,
    RuntimeJob,
    WorkerIdentity,
    WorkerPolicy,
)
from product_api.tenant import PostgresTenantRepository

SIDECAR = Path(__file__).resolve().parents[2] / "pi_sidecar" / "f42_protocol_sidecar.mjs"


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def create_worker() -> DurableWorker:
    identity = WorkerIdentity(
        UUID(_required("SUPPLYAGENT_WORKER_ORGANIZATION_ID")),
        UUID(_required("SUPPLYAGENT_WORKER_ACTOR_ID")),
        _required("SUPPLYAGENT_WORKER_OWNER"),
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

    def engine(job: RuntimeJob) -> PiRuntimeAdapter:
        command = ("node", str(SIDECAR))
        if replay and job.model_policy_id is None:
            return PiRuntimeAdapter(command, timeout_seconds=policy.lease_seconds * 2)
        if job.model_policy_id is None or signer is None or not gateway_url:
            raise HarnessTurnError("model_failed")
        capability = signer.issue(ModelCapability(
            identity.actor_id, identity.organization_id, job.trace_run_id,
            job.model_policy_id, int(time.time()) + min(policy.lease_seconds * 10, 3600),
        ))
        return PiRuntimeAdapter(
            command, timeout_seconds=policy.lease_seconds * 2,
            gateway_url=gateway_url, gateway_capability=capability,
        )

    return DurableWorker(PostgresRuntimeStore(tenants, identity), identity, policy, engine)


async def run_worker(*, once: bool, poll_seconds: float) -> None:
    if poll_seconds <= 0:
        raise ValueError("poll interval must be positive")
    worker = create_worker()
    while True:
        worked = await worker.run_once()
        if once:
            return
        if not worked:
            await asyncio.sleep(poll_seconds)


def main() -> None:
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="Run one organization's durable F35 Worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    arguments = parser.parse_args()
    asyncio.run(run_worker(once=arguments.once, poll_seconds=arguments.poll_seconds))


if __name__ == "__main__":
    main()
