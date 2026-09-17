"""BOM demand expansion; arithmetic is gated by quantity-basis verification."""
from decimal import Decimal, localcontext

from procurement_core.numbers import quantity


def expand_lines(production_qty: Decimal, bom_lines: list[dict],
                 selected: dict[str, str]) -> list[dict]:
    quantity(production_qty, positive=True)
    known = {row["line_id"] for row in bom_lines}
    if set(selected) - known:
        raise ValueError("Selection refers to a line outside this demand BOM")
    result = []
    for row in bom_lines:
        line_id = row["line_id"]
        if not row["qty_basis_verified"]:
            result.append({"line_id": line_id, "component_id": None, "required_qty": None,
                           "unresolved_reason": "quantity_basis_unverified"})
            continue
        with localcontext() as ctx:
            ctx.prec = 60
            required = quantity(quantity(row["quantity"], positive=True) * production_qty,
                                positive=True)
        candidates = {c["component_id"]: c for c in row["candidates"]}
        chosen = selected.get(line_id)
        if chosen is not None and chosen not in candidates:
            raise ValueError("Selected component is not a candidate for this BOM line")
        reason = None
        if not candidates:
            reason = "missing_candidate"
        elif chosen is None:
            reason = "candidate_selection_required"
        elif candidates[chosen]["identity_status"] != "verified":
            reason = "candidate_identity_unverified"
        result.append({"line_id": line_id, "component_id": chosen if reason is None else None,
                       "required_qty": required, "unresolved_reason": reason})
    return result


def prepare_demand(repository, *, demand_id, project_id, product_version, production_qty,
                   need_by_date, created_by, selected=None, tenant_id="default", is_simulated=False):
    """Create and expand one demand atomically; project_id identifies the versioned BOM."""
    quantity(production_qty, positive=True)
    if not all(isinstance(value, str) and value.strip()
               for value in (project_id, product_version, created_by, tenant_id)):
        raise ValueError("Demand project, version, author and tenant are required")
    with repository.atomic():
        repository.create_demand({"demand_id": demand_id, "project_id": project_id,
                                  "product_version": product_version, "quantity": production_qty,
                                  "need_by_date": need_by_date, "created_by": created_by,
                                  "tenant_id": tenant_id, "is_simulated": is_simulated})
        bom_lines = repository.bom(project_id)
        if not bom_lines:
            raise ValueError("Project has no BOM lines")
        lines = expand_lines(production_qty, bom_lines, selected or {})
        repository.save_demand_lines(demand_id, lines)
        return {"demand_id": demand_id, "lines": lines,
                "ready": all(row["unresolved_reason"] is None for row in lines),
                "confirmation_requests": confirmation_requests(bom_lines, lines)}


def confirmation_requests(bom_lines, expanded_lines):
    """Keep original manufacturer/MPN suffixes visible when asking for confirmation."""
    by_id = {row["line_id"]: row for row in bom_lines}
    return [{"line_id": line["line_id"], "reason": line["unresolved_reason"],
             "candidates": [dict(c) for c in by_id[line["line_id"]]["candidates"]]}
            for line in expanded_lines if line["unresolved_reason"] is not None]
