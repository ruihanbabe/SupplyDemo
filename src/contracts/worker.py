"""What crosses the boundary between Supervisor and a Worker.

One rule shapes everything here: workers exchange typed results, never prose (BR-20).
A worker that hands the next one a paragraph forces it to re-parse numbers out of
English, and a number that has been parsed out of a sentence has lost its provenance —
nobody can say afterwards whether 224 came from a supplier or from a model's summary.

So `WorkerResult.content` is a dataclass, not a string. Free text exists in exactly one
place, `narrative`, and it is explicitly marked as unusable for control decisions: it is
for a person to read, never for another worker to act on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

#: Same four states as a tool result, and for the same reason: "there are none",
#: "we only saw part of it" and "the lookup failed" lead to different purchasing
#: decisions and must never be collapsed (BR-05).
Status = Literal["ok", "partial", "not_found", "error"]

#: How much a worker may spend. Wall clock is the one that binds for service workers;
#: tokens bind for agents. Both are declared because a caller cannot know which worker
#: it is dispatching to — that is the point of wrapping workers as tools.
@dataclass(frozen=True)
class Budget:
    max_tokens: int | None = None
    max_seconds: float | None = None
    #: Calls to external systems. Distributor quotas are per account per day, so this is
    #: a real constraint and not a theoretical one.
    max_external_calls: int | None = None

    def child(self, fraction: float) -> Budget:
        """Split a share off for a sub-task.

        Deducted from the parent rather than granted anew: the whole point of a budget is
        that fanning out to nine nodes cannot cost nine times the budget of one (T09).
        """
        return Budget(
            max_tokens=None if self.max_tokens is None else int(self.max_tokens * fraction),
            max_seconds=None if self.max_seconds is None else self.max_seconds * fraction,
            max_external_calls=None if self.max_external_calls is None
            else max(1, int(self.max_external_calls * fraction)))


@dataclass(frozen=True)
class WorkerTask:
    """One bounded piece of work, fully described.

    `inputs` holds references — component ids, evidence ids, snapshot ids — not the data
    itself. A worker fetches what it needs through its own permitted tools, which is what
    keeps "who may read internal stock" answerable per worker rather than per payload
    (T04). Passing the data inline would route it around the permission check.
    """

    task_id: UUID
    worker: str
    #: What this task is for, from a frozen vocabulary rather than free text: an
    #: enumerated purpose can be routed, counted and evaluated; a sentence cannot.
    purpose: str
    inputs: dict[str, Any] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)
    run_id: UUID | None = None
    parent_task_id: UUID | None = None
    trace_id: str | None = None
    #: Set when this task is one branch of a fan-out, so the join can tell a missing
    #: branch from one that was never dispatched.
    branch_key: str | None = None

    @classmethod
    def create(cls, worker: str, purpose: str, **kwargs: Any) -> WorkerTask:
        return cls(task_id=uuid4(), worker=worker, purpose=purpose, **kwargs)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    external_calls: int = 0
    duration_ms: int = 0


@dataclass(frozen=True)
class WorkerResult:
    """What a worker hands back. Typed content, plus how far it got.

    `content` is whatever dataclass that worker produces — a verdict, a comparison, a
    proposal. The dispatcher does not interpret it; only the worker that declared the
    type and the one that consumes it do.
    """

    task_id: UUID
    worker: str
    status: Status
    content: Any = None
    #: Every fact in `content` must be reachable from here. A result that asserts a lead
    #: time and cites nothing is not usable for a purchase decision (BR-07, FR-05).
    evidence_refs: tuple[str, ...] = ()
    #: For a person to read. Never parsed by another worker, never used in a control
    #: condition — that is what `content` is for.
    narrative: str | None = None
    error_code: str | None = None
    message: str | None = None
    usage: Usage = field(default_factory=Usage)
    finished_at: datetime | None = None

    @property
    def usable(self) -> bool:
        """Whether a consumer may build on this.

        `partial` counts: a comparison missing one of three distributors is still worth
        acting on, as long as the gap is carried forward. What must never count is
        `error` — a branch that failed is not a branch that found nothing (D24).
        """
        return self.status in ("ok", "partial")


@dataclass(frozen=True)
class RoutingDecision:
    """Why the Supervisor dispatched what it did.

    Recorded for every request, including the ones that took the simplest path. Without
    it, "why did it not check the datasheet" has no answer, and a routing bug looks
    exactly like a worker that returned nothing (FR-13).
    """

    run_id: UUID
    intent: str
    chosen_workers: tuple[str, ...]
    skipped_workers: tuple[str, ...] = ()
    #: The rule that fired, not a model's explanation of itself.
    basis: str = ""
    fell_back: bool = False
