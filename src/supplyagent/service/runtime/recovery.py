"""Fixed recovery decisions; SQL still owns transitions and retry timing."""

from dataclasses import replace

PERMANENT = frozenset({
    "protocol_failed", "permission_revoked", "binding_invalid", "duplicate_call",
    "persistence_failed", "context_overflow", "context_denied", "context_scope_changed",
    "context_invalid_evidence", "context_history_changed", "context_snapshot_invalid",
    "context_summary_invalid", "resource_unconfigured", "resource_lease_lost",
    "revalidation_failed",
    "channel_config_unavailable", "channel_payload_invalid", "channel_idempotency_conflict",
})


def failure_policy(code, reason, policy):
    # A permanent failure terminates via the existing SQL attempts gate, not a second queue.
    return replace(policy, max_attempts=1) if code in PERMANENT or reason in PERMANENT else policy


def safe_reason(code, reason):
    return reason if reason in PERMANENT | {"service_unavailable", "deadline_exceeded", "channel_outcome_unknown"} else code


class CheckpointHooks:
    """Fixed trusted Python lifecycle, not arbitrary model or user callbacks."""

    def __init__(self, store):
        self.store = store

    async def emit(self, job, phase, detail=None):
        if phase not in {"claimed", "business_completed", "failed", "before_terminal"}:
            raise ValueError("invalid checkpoint phase")
        return await self.store.checkpoint(job, phase, detail or {})
