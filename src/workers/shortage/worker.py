"""`shortage`: expand the BOM for a production run and say which parts are short.

The first node of the procurement graph. Every quantity here was computed by the
procurement core and reaches this worker through a tool, so it carries the same
permission check and audit as anything a model asks for. Lines that still need a person
— a candidate to pick, an identity to confirm — come back as unresolved, never guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from contracts import errors
from contracts.worker import WorkerTask
from workers.base import Outcome, ServiceWorker, WorkerFailure

TOOL = "compute_shortage"


@dataclass(frozen=True)
class ShortComponent:
    component_id: str
    mpn: str
    required_qty: Decimal
    allocatable_qty: Decimal
    shortage_qty: Decimal
    #: The core's own reason codes (eta_unknown, unconfirmed, ...), not prose.
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedLine:
    line_id: str
    #: candidate_selection_required, missing_candidate, ... — what a person must decide.
    reason: str


@dataclass(frozen=True)
class ShortageReport:
    project_id: str
    production_qty: Decimal
    need_by_date: date
    #: The child run holding the persisted shortage snapshots; the numbers above are
    #: copies of what is stored there.
    shortage_run_id: UUID
    components_checked: int
    short: tuple[ShortComponent, ...]
    unresolved: tuple[UnresolvedLine, ...]
    #: Fewer rows came back than were short; the rest are in the snapshots.
    truncated: bool
    provenance: str


class ShortageWorker(ServiceWorker):
    name = "shortage"
    purposes = frozenset({"compute_shortage"})
    content_type = ShortageReport

    def execute(self, task: WorkerTask, *, deadline: float | None) -> Outcome:
        project_id = task.inputs.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise WorkerFailure(errors.BAD_REQUEST, "inputs.project_id is required")
        try:
            production_qty = Decimal(str(task.inputs.get("production_qty")))
        except InvalidOperation:
            raise WorkerFailure(errors.BAD_REQUEST, "inputs.production_qty is not a number") \
                from None
        if not production_qty.is_finite() or production_qty <= 0:
            raise WorkerFailure(errors.BAD_REQUEST, "inputs.production_qty must be positive")

        result = self.call_tool(task, TOOL, {"project_id": project_id,
                                             "production_qty": str(production_qty)})
        if result.status not in ("ok", "partial"):
            return Outcome(status=result.status, error_code=result.error_code,
                           message=result.message)
        body = result.content
        short = tuple(
            ShortComponent(component_id=row["component_id"], mpn=row["mpn"],
                           required_qty=Decimal(row["required_qty"]),
                           allocatable_qty=Decimal(row["allocatable_qty"]),
                           shortage_qty=Decimal(row["shortage_qty"]),
                           reason_codes=tuple(row["reason_codes"]))
            for row in body["rows"])
        return Outcome(
            status=result.status,
            content=ShortageReport(
                project_id=project_id, production_qty=production_qty,
                need_by_date=date.fromisoformat(body["need_by_date"]),
                shortage_run_id=UUID(body["run_id"]),
                components_checked=body["components_checked"],
                short=short,
                unresolved=tuple(UnresolvedLine(row["line_id"], row["reason"])
                                 for row in body["unresolved"]),
                truncated=body["shortage_count"] > len(short),
                provenance=result.provenance),
            narrative=result.message)
