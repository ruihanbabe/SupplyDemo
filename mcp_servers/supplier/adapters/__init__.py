"""Adapter registry and the two things every provider needs around it.

Rate limiting lives here rather than in each adapter because the budget belongs to the
account, not to the code path: Mouser publishes 30 calls a minute and 1000 a day, and a
search plus a quote is two of them. A bucket per provider keeps one busy part number
from spending the day's allowance.

Retries do not live here. They are the registry's job on the parent side (F13) — an
adapter that also retried would multiply the count nobody is watching.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from . import digikey, element14, mouser

ADAPTERS = {module.NAME: module for module in (mouser, element14, digikey)}


@dataclass
class TokenBucket:
    """Requests per period, refilled continuously.

    Refusing locally is the point: a 429 costs a round trip and, with some providers,
    counts against the quota anyway.
    """

    capacity: int
    period_seconds: float
    _tokens: float = 0.0
    _last: float = 0.0

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def take(self, timeout: float = 0.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    float(self.capacity),
                    self._tokens + (now - self._last) * self.capacity / self.period_seconds)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


#: Published limits, not guesses. Where a provider does not state one, a conservative
#: figure is used and the comment says so, because an unstated limit still exists.
BUCKETS = {
    # Mouser publishes 30/minute and 1000/day.
    "mouser": TokenBucket(capacity=30, period_seconds=60),
    # element14 does not publish a public per-minute figure; this is a self-imposed cap.
    "element14": TokenBucket(capacity=30, period_seconds=60),
    # DigiKey returns x-ratelimit-limit: 1000 (per day); 60/minute is self-imposed.
    "digikey": TokenBucket(capacity=60, period_seconds=60),
}
