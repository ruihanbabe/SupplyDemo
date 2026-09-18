"""Plan readback. The stored rows are the exact canonical form that was hashed."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from api.dependencies import Repo
from api.envelope import envelope

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("/{plan_id}")
def read_plan(
    plan_id: UUID,
    request: Request,
    repository: Repo,
) -> dict:
    """content_hash travels with the plan so a reviewer can see what BR-08 binds."""
    return envelope({"plan": repository.read_plan(plan_id)}, trace_id=request.state.trace_id)
