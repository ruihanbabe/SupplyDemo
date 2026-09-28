"""The error vocabulary every backend and tool must speak.

Codes live here rather than as string literals at each raise site for one reason: the
runtime branches on them. A typo in a literal does not fail loudly, it silently lands in
the "unknown failure" branch — and `not_found`, `unknown`, `partial` and `error` mean
four different things to a buyer (ARCHITECTURE.md invariant 3).
"""
from __future__ import annotations

from typing import Final

#: The request asked for something this backend cannot do. Never downgraded silently:
#: dropping an output schema or a tool constraint changes what the answer means.
CAPABILITY_UNSUPPORTED: Final = "capability_unsupported"

#: Configuration is incomplete — a key, an endpoint or a model is missing.
PROVIDER_NOT_CONFIGURED: Final = "provider_not_configured"
#: Real calls cost money and stay off until explicitly authorised.
PROVIDER_DISABLED: Final = "provider_disabled"

UNAUTHORIZED: Final = "unauthorized"
FORBIDDEN: Final = "forbidden"
RATE_LIMITED: Final = "rate_limited"
PROVIDER_ERROR: Final = "provider_error"
BAD_REQUEST: Final = "bad_request"
TIMEOUT: Final = "timeout"
TRANSPORT_ERROR: Final = "transport_error"
MALFORMED_RESPONSE: Final = "malformed_response"

#: Replay was asked for a request it has no recording of. It is an error, never an
#: improvised answer: a replay that invents output cannot be used as a regression
#: baseline, which is the only reason it exists (F20).
FIXTURE_MISSING: Final = "fixture_missing"

#: Retrying these can succeed; the rest cannot and must surface immediately.
RETRYABLE: Final = frozenset({RATE_LIMITED, PROVIDER_ERROR, TIMEOUT, TRANSPORT_ERROR})

#: Worker envelope failures. The envelope turns every one of these into an `error`
#: result instead of letting it propagate: one branch crashing must not take the join
#: down with it, and it must not look like a branch that found nothing either (D24).
WRONG_WORKER: Final = "wrong_worker"
UNKNOWN_PURPOSE: Final = "unknown_purpose"
BUDGET_EXHAUSTED: Final = "budget_exhausted"
WORKER_CRASHED: Final = "worker_crashed"
CONTENT_TYPE_MISMATCH: Final = "content_type_mismatch"
