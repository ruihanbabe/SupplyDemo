"""The envelope every worker runs inside.

Mechanism only: check the task is addressed here, refuse an empty budget, time the run,
turn any failure into a four-state result, hold `content` to its declared type. No
business rule lives here — what a result must cite, what it may cost, which tools it
may reach are decided by the modules that own those rules, so changing one of them does
not mean touching every worker.
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from uuid import uuid4

from contracts import errors
from contracts.llm import ToolCall
from contracts.worker import Status, Usage, WorkerResult, WorkerTask
from tools.registry import Invocation, ToolOutcome, ToolRegistry

log = logging.getLogger(__name__)


class WorkerFailure(Exception):
    """A failure the worker can name. Raised so it can bail out from any depth."""

    def __init__(self, code: str, message: str | None = None, *,
                 usage: Usage | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        #: What was spent before giving up; a failed run still cost something.
        self.usage = usage or Usage()


@dataclass(frozen=True)
class Outcome:
    """What `execute` hands back. The envelope adds identity and timing."""

    status: Status
    content: Any = None
    evidence_refs: tuple[str, ...] = ()
    narrative: str | None = None
    error_code: str | None = None
    message: str | None = None
    #: `duration_ms` is measured by the envelope; anything set here is overwritten.
    usage: Usage = field(default_factory=Usage)


class Worker(ABC):
    name: ClassVar[str]
    kind: ClassVar[Literal["agent", "service"]]
    #: Purposes this worker accepts, from the frozen vocabulary of `WorkerTask`.
    purposes: ClassVar[frozenset[str]]
    #: The type `content` must have when the result is usable.
    content_type: ClassVar[type]

    def run(self, task: WorkerTask) -> WorkerResult:
        """Run one task. Never raises for anything the worker did wrong."""
        started = time.monotonic()
        try:
            outcome = self._checked(self._guarded(task, started))
        except WorkerFailure as exc:
            outcome = Outcome(status="error", error_code=exc.code, message=exc.message,
                              usage=exc.usage)
        except Exception as exc:  # the envelope's whole job
            # Type name only: exception text can carry secrets, and results reach traces.
            log.exception("worker %s crashed on task %s", self.name, task.task_id)
            outcome = Outcome(status="error", error_code=errors.WORKER_CRASHED,
                              message=type(exc).__name__)
        return WorkerResult(
            task_id=task.task_id, worker=self.name, status=outcome.status,
            content=outcome.content if outcome.status != "error" else None,
            evidence_refs=tuple(dict.fromkeys(outcome.evidence_refs)),
            narrative=outcome.narrative, error_code=outcome.error_code,
            message=outcome.message,
            usage=replace(outcome.usage,
                          duration_ms=int((time.monotonic() - started) * 1000)),
            finished_at=datetime.now(UTC))

    def _guarded(self, task: WorkerTask, started: float) -> Outcome:
        if task.worker != self.name:
            raise WorkerFailure(errors.WRONG_WORKER, f"task addressed to {task.worker!r}")
        if task.purpose not in self.purposes:
            raise WorkerFailure(errors.UNKNOWN_PURPOSE, task.purpose)
        budget = task.budget
        if (budget.max_seconds is not None and budget.max_seconds <= 0) \
                or budget.max_tokens == 0 or budget.max_external_calls == 0:
            raise WorkerFailure(errors.BUDGET_EXHAUSTED)
        deadline = None if budget.max_seconds is None else started + budget.max_seconds
        return self.execute(task, deadline=deadline)

    def _checked(self, outcome: Outcome) -> Outcome:
        if outcome.status in ("ok", "partial") \
                and not isinstance(outcome.content, self.content_type):
            raise WorkerFailure(errors.CONTENT_TYPE_MISMATCH,
                                f"expected {self.content_type.__name__}, "
                                f"got {type(outcome.content).__name__}",
                                usage=outcome.usage)
        return outcome

    @abstractmethod
    def execute(self, task: WorkerTask, *, deadline: float | None) -> Outcome:
        """Produce the content. `deadline` is on the `time.monotonic()` clock."""


class ServiceWorker(Worker):
    """Deterministic code; never calls a model. Reaches data only through the registry."""

    kind = "service"

    def __init__(self, registry: ToolRegistry,
                 invocation: Callable[[WorkerTask], Invocation]) -> None:
        self._registry = registry
        self._invocation = invocation

    def call_tool(self, task: WorkerTask, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """One tool call, through the same permission check and audit as a model's."""
        call = ToolCall(id=str(uuid4()), name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False))
        return self._registry.dispatch(call, self._invocation(task))
