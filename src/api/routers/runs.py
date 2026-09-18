"""Run lifecycle plus the two deterministic computations it drives.

These endpoints write only internal advisory records (shortage_snapshot, plan). They
create no external side effects, so BR-05 applies rather than the approval gate: no
stock is reserved and nothing here needs a PermissionDecision. External writes (FR-07)
are a later slice and will not reuse these routes.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Request

from api.dependencies import Repo
from api.envelope import envelope
from api.schemas import PlanCreate, RunCreate
from procurement_core.plans import generate_plan
from procurement_core.shortage import calculate_shortages

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.post("")
def create_run(
    payload: RunCreate,
    request: Request,
    repository: Repo,
) -> dict:
    demand = repository.read_demand(payload.demand_id)
    run = repository.create_run({
        "run_id": uuid4(),
        "tenant_id": demand["tenant_id"],
        "demand_id": payload.demand_id,
        "trigger_kind": payload.trigger_kind,
        "state": "created",
        "budget_total": payload.budget_total,
        "parent_run_id": payload.parent_run_id,
    })
    return envelope({"run": run}, trace_id=request.state.trace_id)


@router.post("/{run_id}/shortages")
def compute_shortages(
    run_id: UUID,
    request: Request,
    repository: Repo,
) -> dict:
    result = calculate_shortages(repository, run_id)
    # Snapshot warnings are evidence, not decoration: EV-02's resource competition and a
    # missing inventory snapshot both surface here rather than being smoothed over.
    warnings = sorted({
        warning
        for snapshot in result["snapshots"]
        for warning in snapshot["breakdown"]["warnings"]
    })
    if result["unresolved_lines"]:
        warnings.append("unresolved_lines_excluded_from_calculation")
    return envelope(
        result,
        status="ok" if result["ready"] else "partial",
        warnings=warnings,
        trace_id=request.state.trace_id,
    )


@router.post("/{run_id}/plans")
def create_plan(
    run_id: UUID,
    payload: PlanCreate,
    request: Request,
    repository: Repo,
) -> dict:
    result = generate_plan(
        repository,
        run_id=run_id,
        snapshot_ids=payload.snapshot_ids,
        offers=payload.domain_offers(),
    )
    return envelope(
        result,
        status="ok" if result["ready_for_review"] else "partial",
        warnings=[] if result["ready_for_review"] else ["plan_not_ready_for_review"],
        trace_id=request.state.trace_id,
    )
