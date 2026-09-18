"""Plan readback. The stored rows are the exact canonical form that was hashed."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from api.dependencies import Repo
from api.envelope import envelope
from procurement_core.plans import canonical

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("/{plan_id}")
def read_plan(
    plan_id: UUID,
    request: Request,
    repository: Repo,
) -> dict:
    """content_hash travels with the plan so a reviewer can see what BR-08 binds.

    Line values are returned in the canonical form the hash was computed over, not in the
    column's storage scale. Otherwise creating a plan would answer "30" and reading it
    back "30.000000", and a reviewer recomputing the hash from the readback would get a
    different digest than the one stored — which would defeat the point of storing it.
    """
    plan = repository.read_plan(plan_id)
    plan["lines"] = canonical(plan["lines"])
    return envelope({"plan": plan}, trace_id=request.state.trace_id)
