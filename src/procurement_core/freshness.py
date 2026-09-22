"""Whether an observation is still usable, judged at read time.

Deliberately not a column on evidence. Baking an expiry at write time would mean a
policy change requires rewriting history, and would make it impossible to ask "under
today's rule, was that conclusion still sound?" — which is the whole point of keeping
old evidence (F20).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

#: usable  — within the window
#: stale   — past the window; must be re-fetched or escalated, never silently used
#: unknown — no policy is in force, so freshness cannot be judged. Not the same as
#:           stale: one means "too old", the other means "nobody said how old is too old"
Verdict = Literal["usable", "stale", "unknown"]

FRESHNESS_RULE = "evidence.freshness_window"


@dataclass(frozen=True)
class FreshnessPolicy:
    """Per-kind maximum age, and the rule version it came from."""

    windows: dict[str, timedelta]
    version: int | None = None

    @classmethod
    def from_rule(cls, rule: dict | None) -> FreshnessPolicy:
        if rule is None:
            return cls(windows={})
        hours = (rule.get("value") or {}).get("hours") or {}
        return cls(windows={kind: timedelta(hours=float(h)) for kind, h in hours.items()},
                   version=rule.get("version"))


def judge(retrieved_at: datetime, kind: str, policy: FreshnessPolicy,
          at: datetime) -> Verdict:
    """Three outcomes, never two.

    Collapsing `unknown` into `stale` would make an unconfigured system look like one
    whose data has expired, and a buyer would go re-fetch data that was never the
    problem.
    """
    window = policy.windows.get(kind)
    if window is None:
        return "unknown"
    return "usable" if at - retrieved_at <= window else "stale"
