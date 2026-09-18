"""Read-only catalogue: projects and their BOM lines with candidates."""
from __future__ import annotations

from fastapi import APIRouter, Request

from api.dependencies import Repo
from api.envelope import envelope

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/projects")
def list_projects(
    request: Request,
    repository: Repo,
) -> dict:
    projects = repository.list_projects()
    return envelope(
        {"projects": projects},
        status="ok" if projects else "not_found",
        trace_id=request.state.trace_id,
    )


@router.get("/projects/{project_id}/bom")
def read_bom(
    project_id: str,
    request: Request,
    repository: Repo,
) -> dict:
    """Candidates travel with each line: BR-02 selection happens against this list."""
    lines = repository.bom(project_id)
    warnings = []
    # Flags the core already computed; the router reports them, it does not decide them.
    if any(not line["qty_basis_verified"] for line in lines):
        warnings.append("quantity_basis_unverified_lines_present")
    if any(not line["candidates"] for line in lines):
        warnings.append("lines_without_candidates_present")
    if any(len(line["candidates"]) > 1 for line in lines):
        warnings.append("multi_candidate_lines_require_selection")
    return envelope(
        {"project_id": project_id, "lines": lines},
        status="ok" if lines else "not_found",
        warnings=warnings,
        trace_id=request.state.trace_id,
    )
