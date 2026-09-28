"""Internal stock, read one component at a time.

Kept apart from the catalogue tools because it needs its own permission: who may see
internal stock is a real boundary (spec_check may not), and a permission shared with the
BOM catalogue could not express it.

Quantities come from the procurement core's `availability`, the same arithmetic the
shortage calculation uses — two places computing "how many are there" would drift.
Nothing here computes a shortage.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from contracts.llm import ToolSpec
from procurement_core.shortage import availability
from tools.registry import RegisteredTool, ToolOutcome

TENANT = "default"


def _line_alternates(arguments: dict[str, Any], context) -> ToolOutcome:
    """The other candidates on every BOM line where this component is a candidate."""
    project_id = str(arguments["project_id"])
    component_id = str(arguments["component_id"])
    lines = [line for line in context.repository.bom(project_id)
             if any(c["component_id"] == component_id for c in line["candidates"])]
    if not lines:
        return ToolOutcome("not_found", error_code="component_not_in_project",
                           message=f"{component_id} is not a candidate in {project_id}")
    content = {"project_id": project_id, "component_id": component_id, "lines": [
        {"line_id": line["line_id"], "alternates": [
            {"component_id": c["component_id"], "mpn": c["mpn"],
             "manufacturer": c["manufacturer"]}
            for c in line["candidates"] if c["component_id"] != component_id]}
        for line in lines]}
    return ToolOutcome("ok", content=content)


def _material_availability(arguments: dict[str, Any], context) -> ToolOutcome:
    component_id = str(arguments["component_id"])
    try:
        need_by = date.fromisoformat(str(arguments["need_by_date"]))
    except ValueError:
        return ToolOutcome("error", error_code="invalid_arguments",
                           message="need_by_date must be YYYY-MM-DD")
    supply = context.repository.supply_snapshot(TENANT)
    rows = {kind: [row for row in supply[kind] if row["component_id"] == component_id]
            for kind in ("inventory", "allocations", "transit")}
    if not rows["inventory"]:
        # No snapshot is not zero stock. Unknown stays unknown (FR-02).
        return ToolOutcome("not_found", error_code="inventory_snapshot_missing",
                           message=f"No inventory snapshot for {component_id}")
    result = availability(None, need_by, rows["inventory"], rows["allocations"], rows["transit"])
    simulated = any(row.get("is_simulated") for group in rows.values() for row in group)
    content = {
        "component_id": component_id,
        "need_by_date": need_by.isoformat(),
        "on_hand_qty": str(result["on_hand"]),
        "quarantine_qty": str(sum(row["quarantine_qty"] for row in rows["inventory"])),
        "allocated_to_others_qty": str(result["other"]),
        "incoming_in_time_qty": str(result["incoming"]),
        "available_qty": str(result["available"]),
        "excluded_transit": [{"qty": str(row["qty"]), "reason": row["excluded_reason"]}
                             for row in result["excluded"]],
        "snapshot_at": max(str(row["snapshot_at"]) for row in rows["inventory"]),
    }
    return ToolOutcome("ok", content=content, provenance="sample" if simulated else "real")


INVENTORY_TOOLS = (
    RegisteredTool(
        spec=ToolSpec(
            name="get_line_alternates",
            description="列出某元件所在 BOM 行上的其他候选型号。候选不等于技术等价。",
            parameters={"type": "object",
                        "properties": {"project_id": {"type": "string"},
                                       "component_id": {"type": "string"}},
                        "required": ["project_id", "component_id"]}),
        effect="read", handler=_line_alternates, permission="catalog.read",
        scenarios=frozenset({"procurement"})),
    RegisteredTool(
        spec=ToolSpec(
            name="get_material_availability",
            description=("查询单个元件的内部库存、占用与按期在途，返回可用量。"
                         "不计算缺口。当前库存为模拟数据。"),
            parameters={"type": "object",
                        "properties": {"component_id": {"type": "string"},
                                       "need_by_date": {"type": "string",
                                                        "description": "YYYY-MM-DD"}},
                        "required": ["component_id", "need_by_date"]}),
        effect="read", handler=_material_availability, permission="inventory.read",
        scenarios=frozenset({"procurement"})),
)
