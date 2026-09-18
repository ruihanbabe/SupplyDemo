"""Demand submission (FR-01) and BOM expansion readback (FR-02)."""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Request

from api.dependencies import Repo
from api.envelope import envelope
from api.schemas import DemandCreate
from procurement_core.demand import prepare_demand

router = APIRouter(prefix="/api/demands", tags=["demands"])


@router.post("")
def create_demand(
    payload: DemandCreate,
    request: Request,
    repository: Repo,
) -> dict:
    """Identity is allocated server-side; a client-supplied ID would let callers collide."""
    result = prepare_demand(
        repository,
        demand_id=uuid4(),
        project_id=payload.project_id,
        product_version=payload.product_version,
        production_qty=payload.production_qty,
        need_by_date=payload.need_by_date,
        created_by=payload.created_by,
        selected=payload.selected,
        tenant_id=payload.tenant_id,
        is_simulated=payload.is_simulated,
    )
    # Partial is the honest status for a mixed order: BR-10 forbids marking it ready.
    return envelope(
        result,
        status="ok" if result["ready"] else "partial",
        warnings=[] if result["ready"] else ["unresolved_lines_require_confirmation"],
        trace_id=request.state.trace_id,
    )


@router.get("/{demand_id}")
def read_demand(
    demand_id: UUID,
    request: Request,
    repository: Repo,
) -> dict:
    demand = repository.read_demand(demand_id)
    lines = repository.demand_lines(demand_id)
    unresolved = [line for line in lines if line["unresolved_reason"] is not None]
    return envelope(
        {"demand": demand, "lines": lines, "unresolved_lines": unresolved,
         "ready": not unresolved},
        status="ok" if not unresolved else "partial",
        warnings=[] if not unresolved else ["unresolved_lines_require_confirmation"],
        trace_id=request.state.trace_id,
    )


@router.get("/{demand_id}/plans")
def list_plans(
    demand_id: UUID,
    request: Request,
    repository: Repo,
) -> dict:
    plans = repository.demand_plans(demand_id)
    return envelope(
        {"demand_id": demand_id, "plans": plans},
        status="ok" if plans else "not_found",
        trace_id=request.state.trace_id,
    )
