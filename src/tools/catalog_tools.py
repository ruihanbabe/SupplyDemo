"""Read-only tools over the deterministic procurement core.

Every number these return was computed or stored by code, never by a model. That is the
whole point of routing the model through tools instead of letting it answer from the
prompt: the model may choose *which* question to ask, never *what the answer is*
(ARCHITECTURE.md invariant 1).

Payloads are kept deliberately small. A tool result becomes the next request's context,
so returning a whole BOM would spend the budget that the actual reasoning needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.llm import ToolSpec
from tools.registry import RegisteredTool, ToolOutcome, ToolRegistry

#: How many lines a summary may name before it stops being a summary.
SAMPLE_LINES = 8


@dataclass
class ToolContext:
    """What a tool is allowed to reach. Nothing wider than this is in scope."""

    repository: Any


def _list_projects(_: dict[str, Any], context: ToolContext) -> ToolOutcome:
    projects = context.repository.list_projects()
    if not projects:
        return ToolOutcome("not_found", render="projects", content=[],
                           message="No projects are loaded")
    return ToolOutcome("ok", render="projects", content=[
        {"project_id": row["project_id"], "bom_lines": row["bom_lines"],
         "total_quantity": row["total_quantity"]}
        for row in projects])


def _bom_summary(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    project_id = str(arguments["project_id"])
    lines = context.repository.bom(project_id)
    if not lines:
        return ToolOutcome("not_found", render="bom_summary", content=None,
                           message=f"No BOM for project {project_id!r}")
    unverified = [line["line_id"] for line in lines if not line["qty_basis_verified"]]
    no_candidate = [line["line_id"] for line in lines if not line["candidates"]]
    multi = [line["line_id"] for line in lines if len(line["candidates"]) > 1]
    summary = {
        "project_id": project_id,
        "line_count": len(lines),
        "lines_without_candidates": len(no_candidate),
        "lines_needing_selection": len(multi),
        "lines_with_unverified_quantity_basis": len(unverified),
        "sample": [
            {"line_id": line["line_id"], "reference": line["reference"],
             "quantity": str(line["quantity"]),
             "candidate_mpns": [c["mpn"] for c in line["candidates"][:3]]}
            for line in lines[:SAMPLE_LINES]],
    }
    # partial, not ok: the caller is seeing a sample and must not summarise it as if it
    # had seen every line. Collapsing the two is exactly what invariant 3 forbids.
    status = "partial" if len(lines) > SAMPLE_LINES else "ok"
    return ToolOutcome(status, render="bom_summary", content=summary,
                       message=None if status == "ok"
                       else f"Showing {SAMPLE_LINES} of {len(lines)} lines")


def _refuse_draft(_: dict[str, Any], __: ToolContext) -> ToolOutcome:  # pragma: no cover
    raise AssertionError("unreachable: the registry refuses this tool before dispatch")


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(RegisteredTool(
        spec=ToolSpec(
            name="list_projects",
            description="列出已导入的项目及其 BOM 行数与总用量。数据来自数据库，不是估算。",
            parameters={"type": "object", "properties": {}, "required": []}),
        effect="read", handler=_list_projects))
    registry.register(RegisteredTool(
        spec=ToolSpec(
            name="get_bom_summary",
            description=("按项目返回 BOM 概况：行数、缺候选行数、需人工选型行数、"
                         "用量基准未核验行数，以及前几行样例。"),
            parameters={"type": "object",
                        "properties": {"project_id": {
                            "type": "string",
                            "description": "项目标识，可先用 list_projects 取得"}},
                        "required": ["project_id"]}),
        effect="read", handler=_bom_summary))
    # Declared so the boundary is visible and testable, and refused for a named reason.
    registry.register(RegisteredTool(
        spec=ToolSpec(
            name="create_procurement_draft",
            description="在外部采购系统创建草稿。需人工批准；当前未接入。",
            parameters={"type": "object",
                        "properties": {"plan_id": {"type": "string"}},
                        "required": ["plan_id"]}),
        effect="write", handler=_refuse_draft, available=False,
        unavailable_reason="外部采购系统与其幂等能力尚未确定（Q-03）；在此之前不做任何外部写入"))
    return registry
