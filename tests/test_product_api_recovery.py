"""Permanent failures, attempt-fenced checkpoints and scoped capabilities."""

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from test_product_api_runtime import PortEngine, _worker

from product_api.gateway import CapabilitySigner, ModelCapability
from product_api.harness import HarnessTurnError
from product_api.recovery import failure_policy


@pytest.mark.parametrize("reason", ["permission_revoked", "binding_invalid", "context_overflow"])
def test_permanent_cause_stops_retry_and_is_checkpointed(reason):
    async def scenario():
        worker, store = _worker()
        worker._policy = replace(worker._policy, max_attempts=5)

        class Failed(PortEngine):
            async def run_turn(self, *args, **kwargs):
                raise HarnessTurnError("model_failed", reason=reason)

        worker._engine_factory = lambda job: Failed()
        report = await worker.run_once_report()
        assert report.outcome == "failed" and store.state == "failed"
        assert store.checkpoints == [
            ("claimed", {}), ("failed", {"code": "model_failed", "reason": reason, "retryable": False})
        ]
    asyncio.run(scenario())


def test_checkpoint_commit_failure_never_reports_completed():
    async def scenario():
        worker, store = _worker()

        async def unavailable(*args):
            raise OSError("synthetic storage failure")

        store.checkpoint = unavailable
        report = await worker.run_once_report()
        assert report.outcome == "process_error" and store.state != "completed"
    asyncio.run(scenario())


def test_transient_failure_keeps_configured_retry_policy():
    worker, _ = _worker()
    assert failure_policy("timeout", None, worker._policy) is worker._policy


def test_job_bound_capability_roundtrip_preserves_all_binding_fields():
    signer = CapabilitySigner(b"synthetic-key-that-is-at-least-32-bytes")
    capability = ModelCapability(uuid4(), uuid4(), uuid4(), uuid4(), int(time.time()) + 60,
                                 uuid4(), 3, "worker-instance")
    assert signer.verify(signer.issue(capability)) == capability
    with pytest.raises(ValueError):
        signer.issue(replace(capability, generation=None))
