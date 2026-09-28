"""`internal`: how many of this part's alternates are already in the building.

The shortage calculation has already counted this component's own stock and transit;
asking again would add nothing. What it cannot see is the other candidates listed on
the same BOM line. This worker reports their availability and stops there — whether an
alternate may replace the part is a person's engineering call (requirements §8).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from contracts import errors
from contracts.worker import Status, WorkerTask
from workers.base import Outcome, ServiceWorker, WorkerFailure

LINES_TOOL = "get_line_alternates"
STOCK_TOOL = "get_material_availability"


@dataclass(frozen=True)
class AlternateStock:
    line_id: str
    component_id: str
    mpn: str
    manufacturer: str
    #: The lookup's own state: not_found here means no inventory snapshot, not zero.
    status: Status
    available_qty: Decimal | None = None
    on_hand_qty: Decimal | None = None
    #: Why some transit did not count (eta_unknown, unconfirmed, ...).
    excluded_reasons: tuple[str, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class AlternateAvailability:
    component_id: str
    need_by_date: str
    alternates: tuple[AlternateStock, ...]
    provenance: str


class InternalWorker(ServiceWorker):
    name = "internal"
    purposes = frozenset({"alternate_availability"})
    content_type = AlternateAvailability

    def execute(self, task: WorkerTask, *, deadline: float | None) -> Outcome:
        inputs = task.inputs
        missing = [key for key in ("project_id", "component_id", "need_by_date")
                   if not isinstance(inputs.get(key), str) or not inputs.get(key)]
        if missing:
            raise WorkerFailure(errors.BAD_REQUEST, f"inputs missing: {', '.join(missing)}")
        component_id = inputs["component_id"]

        lines = self.call_tool(task, LINES_TOOL, {"project_id": inputs["project_id"],
                                                  "component_id": component_id})
        if lines.status != "ok":
            return Outcome(status=lines.status, error_code=lines.error_code,
                           message=lines.message)
        candidates = [(line["line_id"], alt) for line in lines.content["lines"]
                      for alt in line["alternates"]]
        if not candidates:
            return Outcome(status="not_found", error_code="no_alternates",
                           message="single-candidate line: nothing to substitute")

        found, provenances = [], set()
        for line_id, alt in candidates:
            stock = self.call_tool(task, STOCK_TOOL, {"component_id": alt["component_id"],
                                                      "need_by_date": inputs["need_by_date"]})
            body = stock.content if stock.status == "ok" else None
            if body is not None:
                provenances.add(stock.provenance)
            found.append(AlternateStock(
                line_id=line_id, component_id=alt["component_id"], mpn=alt["mpn"],
                manufacturer=alt["manufacturer"], status=stock.status,
                available_qty=Decimal(body["available_qty"]) if body else None,
                on_hand_qty=Decimal(body["on_hand_qty"]) if body else None,
                excluded_reasons=tuple(sorted({row["reason"]
                                               for row in body["excluded_transit"]}))
                if body else (),
                error_code=stock.error_code))

        if not any(item.status == "ok" for item in found):
            failed = any(item.status == "error" for item in found)
            return Outcome(status="error" if failed else "not_found",
                           error_code="alternate_lookup_failed" if failed
                           else "inventory_snapshot_missing")
        return Outcome(
            status="ok" if all(item.status == "ok" for item in found) else "partial",
            content=AlternateAvailability(
                component_id=component_id, need_by_date=inputs["need_by_date"],
                alternates=tuple(found),
                provenance="sample" if "sample" in provenances else "real"))
