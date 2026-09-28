"""Read-only tools over the deterministic procurement core.

Every number these return was computed or stored by code, never by a model. That is the
point of routing the model through tools: it may choose *which* question to ask, never
*what the answer is* (BR-07).

Payloads stay small. A tool result becomes the next request's context, so returning a
whole BOM would spend the budget the actual reasoning needs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from contracts.llm import ToolSpec
from permissions.policy import load_policy
from procurement_core.demand import prepare_demand
from procurement_core.shortage import calculate_shortages
from tools.inventory_tools import INVENTORY_TOOLS
from tools.registry import RegisteredTool, RetryPolicy, ToolOutcome, ToolRegistry
from tools.sourcing_tools import SOURCING_TOOLS

#: How many lines a summary may name before it stops being a summary.
SAMPLE_LINES = 8
#: How many shortage rows travel back to the model at once.
MAX_SHORTAGE_ROWS = 10
#: Used when the caller does not state a need-by date. Long enough that in-transit stock
#: with a near ETA still counts, short enough that a far one does not.
DEFAULT_HORIZON_DAYS = 60

#: Why a component came out short, in the words a buyer would use. Translated from the
#: exclusion reasons the deterministic core already recorded — never re-derived here,
#: because two places computing the same explanation will eventually disagree.
EXCLUSION_WORDS = {
    "eta_unknown": "在途无到货日期",
    "unconfirmed": "在途未确认",
    "after_need_by_date": "在途到货晚于需求日",
    "allocated_to_other_demand": "在途已指派给其他需求",
}
WARNING_WORDS = {
    "inventory_snapshot_missing": "无库存快照",
    "resource_competition": "库存被其他需求占用",
    "allocations_exceed_on_hand": "占用量超过实物库存",
}


@dataclass
class ToolContext:
    """What a tool is allowed to reach. Nothing wider than this is in scope."""

    repository: Any
    run_id: Any = None


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
    # had seen every line. Collapsing the two is what BR-05 forbids.
    status = "partial" if len(lines) > SAMPLE_LINES else "ok"
    return ToolOutcome(status, render="bom_summary", content=summary,
                       message=None if status == "ok"
                       else f"Showing {SAMPLE_LINES} of {len(lines)} lines")


def _reason_codes(breakdown: dict[str, Any]) -> list[str]:
    """The core's own reason codes, for callers that branch on them rather than read."""
    codes = {row.get("excluded_reason") for row in breakdown.get("excluded_transit") or []}
    codes |= {warning for warning in breakdown.get("warnings") or [] if warning in WARNING_WORDS}
    return sorted(code for code in codes if code)


def _why_short(breakdown: dict[str, Any]) -> str:
    reasons = {EXCLUSION_WORDS.get(code, WARNING_WORDS.get(code, code))
               for code in _reason_codes(breakdown)}
    words = sorted(reason for reason in reasons if reason)
    # Nothing was excluded and nothing was flagged: the stock on hand plus what arrives
    # in time simply does not cover the demand. Saying so beats an empty cell, which
    # reads as "we do not know why" when in fact we do.
    return "，".join(words) if words else "库存与按期在途合计不足"


def _compute_shortage(arguments: dict[str, Any], context: ToolContext) -> ToolOutcome:
    """Expand a BOM for a production run and compute per-component shortage.

    Single-candidate lines are selected automatically; lines with a real choice are left
    unresolved for a human. Auto-selecting where the choice is unique is not choosing for
    someone (EV-07): there was nothing to choose.
    """
    project_id = str(arguments["project_id"])
    try:
        production_qty = Decimal(str(arguments["production_qty"]))
    except (InvalidOperation, TypeError):
        return ToolOutcome("error", error_code="invalid_arguments",
                           message="production_qty must be a number")
    repository = context.repository
    lines = repository.bom(project_id)
    if not lines:
        return ToolOutcome("not_found", render="shortage", content=None,
                           message=f"No BOM for project {project_id!r}")
    selected = {row["line_id"]: row["candidates"][0]["component_id"]
                for row in lines if len(row["candidates"]) == 1}
    need_by = (datetime.now(UTC) + timedelta(days=DEFAULT_HORIZON_DAYS)).date()

    demand_id = uuid4()
    expansion = prepare_demand(
        repository, demand_id=demand_id, project_id=project_id,
        product_version=str(arguments.get("product_version") or project_id),
        production_qty=production_qty, need_by_date=need_by, created_by="chat",
        selected=selected, is_simulated=True)
    if not any(row["unresolved_reason"] is None for row in expansion["lines"]):
        blockers = sorted({row["unresolved_reason"] for row in expansion["lines"]})
        return ToolOutcome("error", render="shortage", error_code="expansion_blocked",
                           message=f"No line could be expanded: {', '.join(blockers)}")

    # A child run: the shortage is its own computation, and its budget will later be
    # deducted from the conversation's (F29).
    run = repository.create_run({
        "run_id": uuid4(), "tenant_id": "default", "demand_id": demand_id,
        "trigger_kind": "user", "state": "analyzing",
        "budget_total": None, "parent_run_id": context.run_id})
    result = calculate_shortages(repository, run["run_id"])

    short = sorted((snapshot for snapshot in result["snapshots"]
                    if snapshot["shortage_qty"] > 0),
                   key=lambda snapshot: -snapshot["shortage_qty"])
    names = {row["component_id"]: row["mpn"] for row in repository.components(
        [snapshot["component_id"] for snapshot in short])}
    rows = [{"component_id": snapshot["component_id"],
             "mpn": names.get(snapshot["component_id"], snapshot["component_id"]),
             "required_qty": str(snapshot["required_qty"]),
             "allocatable_qty": str(snapshot["allocatable_qty"]),
             "shortage_qty": str(snapshot["shortage_qty"]),
             "why": _why_short(snapshot["breakdown"]),
             "reason_codes": _reason_codes(snapshot["breakdown"])}
            for snapshot in short[:MAX_SHORTAGE_ROWS]]
    unresolved = [row for row in expansion["lines"] if row["unresolved_reason"] is not None]
    content = {"project_id": project_id, "production_qty": str(production_qty),
               "need_by_date": need_by.isoformat(), "run_id": str(run["run_id"]),
               "components_checked": len(result["snapshots"]),
               "unresolved_lines": len(unresolved),
               "unresolved": [{"line_id": row["line_id"], "reason": row["unresolved_reason"]}
                              for row in unresolved],
               "shortage_count": len(short),
               "policy_versions": expansion["policy_versions"], "rows": rows}
    status = "ok" if not unresolved and len(short) <= MAX_SHORTAGE_ROWS else "partial"
    message = None
    if unresolved:
        message = f"{len(unresolved)} 行未能展开，未计入缺口"
    elif len(short) > MAX_SHORTAGE_ROWS:
        message = f"缺口共 {len(short)} 项，显示前 {MAX_SHORTAGE_ROWS} 项"
    # Simulated stock produced these numbers; the label travels with them (BR-09).
    return ToolOutcome(status, render="shortage", content=content, message=message,
                       provenance="sample")


def _refuse_draft(_: dict[str, Any], __: ToolContext) -> ToolOutcome:  # pragma: no cover
    raise AssertionError("unreachable: the registry refuses this tool before dispatch")


def layered_permission_check(tool, invocation) -> str | None:
    """Wire the three-layer chain into the registry.

    The layer travels in the message: "denied" alone does not tell anyone which file to
    open, and that is the whole reason the chain records where a decision came from.
    """
    policy = load_policy(invocation.worker, session=invocation.session)
    decision = policy.evaluate(tool.permission, effect=tool.effect)
    return None if decision.allowed else f"[{decision.layer}] {decision.reason}"


def build_registry(*, audit=None) -> ToolRegistry:
    registry = ToolRegistry(audit=audit, permission_check=layered_permission_check)
    registry.register(RegisteredTool(
        spec=ToolSpec(
            name="list_projects",
            description="列出已导入的项目及其 BOM 行数与总用量。数据来自数据库，不是估算。",
            parameters={"type": "object", "properties": {}, "required": []}),
        effect="read", handler=_list_projects,
        permission="catalog.read", scenarios=frozenset({"procurement"})))
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
        effect="read", handler=_bom_summary,
        permission="catalog.read", scenarios=frozenset({"procurement"})))
    registry.register(RegisteredTool(
        spec=ToolSpec(
            name="compute_shortage",
            description=("按项目与生产数量计算每个元件的缺口。数量由确定性代码算出，"
                         "结果含缺口原因。当前库存为模拟数据。"),
            parameters={"type": "object",
                        "properties": {
                            "project_id": {"type": "string"},
                            "production_qty": {"type": "number",
                                               "description": "生产套数，正数"}},
                        "required": ["project_id", "production_qty"]}),
        effect="read", handler=_compute_shortage,
        permission="procurement.compute", idempotency="idempotent",
        timeout_seconds=60.0, retry_policy=RetryPolicy(attempts=1),
        scenarios=frozenset({"procurement"})))
    registry.register(RegisteredTool(
        spec=ToolSpec(
            name="create_procurement_draft",
            description="在外部采购系统创建草稿。需人工批准；当前未接入。",
            parameters={"type": "object",
                        "properties": {"plan_id": {"type": "string"}},
                        "required": ["plan_id"]}),
        effect="write", handler=_refuse_draft,
        permission="procurement.write", idempotency="unsafe",
        scenarios=frozenset({"procurement"}),
        available=False,
        unavailable_reason="外部采购系统与其幂等能力尚未确定（Q-03）；在此之前不做任何外部写入"))
    for tool in SOURCING_TOOLS + INVENTORY_TOOLS:
        registry.register(tool)
    return registry
