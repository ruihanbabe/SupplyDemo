"""`sourcing`: ask every distributor about one part, keep each answer apart.

The content carries which sources answered and the evidence each one produced, not the
quoted values themselves. Values live in the ledger; a consumer reads them there through
its own permitted tools, so the numbers exist in one place with their provenance.
"""
from __future__ import annotations

from dataclasses import dataclass

from contracts import errors
from contracts.worker import Status, WorkerTask
from workers.base import Outcome, ServiceWorker, WorkerFailure

TOOL = "compare_supplier_offers"


@dataclass(frozen=True)
class SourceAnswer:
    provider: str
    status: Status
    evidence_ids: tuple[str, ...] = ()
    #: The distributor's own match verdict; anything but "exact" needs a person.
    match_status: str | None = None
    provenance: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class OfferComparison:
    mpn: str
    answers: tuple[SourceAnswer, ...]
    stock_disagreement: bool
    prices_comparable: bool
    currencies: tuple[str, ...]


class SourcingWorker(ServiceWorker):
    name = "sourcing"
    purposes = frozenset({"compare_offers"})
    content_type = OfferComparison

    def execute(self, task: WorkerTask, *, deadline: float | None) -> Outcome:
        mpn = task.inputs.get("mpn")
        if not isinstance(mpn, str) or not mpn:
            raise WorkerFailure(errors.BAD_REQUEST, "inputs.mpn is required")
        result = self.call_tool(task, TOOL, {"mpn": mpn})
        if result.status not in ("ok", "partial"):
            return Outcome(status=result.status, error_code=result.error_code,
                           message=result.message)
        body = result.content
        answers = tuple(
            SourceAnswer(provider=row["provider"], status=row["status"],
                         evidence_ids=tuple(row.get("evidence_ids") or ()),
                         match_status=row.get("match_status"),
                         provenance=row.get("provenance"),
                         error_code=row.get("error_code"))
            for row in body["rows"])
        return Outcome(
            status=result.status,
            content=OfferComparison(mpn=mpn, answers=answers,
                                    stock_disagreement=body["stock_disagreement"],
                                    prices_comparable=body["prices_comparable"],
                                    currencies=tuple(body["currencies"])),
            evidence_refs=tuple(ref for answer in answers for ref in answer.evidence_ids),
            narrative=result.message)
