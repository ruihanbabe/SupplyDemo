"""Evidence-backed inventory arithmetic. Suggestions never reserve stock."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

from procurement_core.numbers import quantity


def unique(rows, keys):
    seen = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        if key in seen and seen[key] != row:
            raise ValueError("Conflicting snapshots for the same supply record")
        seen[key] = row
    return list(seen.values())


def compute_shortage(required_qty, demand_id, need_by_date, inventory, allocations, transit):
    """Inputs are for one component and tenant; quantities remain Decimal throughout."""
    quantity(required_qty, positive=True)
    inventory = unique(inventory, ("warehouse",))
    allocations = unique(allocations, ("demand_id", "component_id"))
    transit = unique(transit, ("in_transit_id",))
    with localcontext() as ctx:
        ctx.prec = 60
        on_hand = sum((quantity(r["on_hand_qty"]) for r in inventory), Decimal(0))
        other = sum((quantity(r["allocated_qty"]) for r in allocations
                     if str(r["demand_id"]) != str(demand_id)), Decimal(0))
        available_stock = max(Decimal(0), on_hand - other)
        eligible, committed, excluded = [], [], []
        for row in transit:
            quantity(row["qty"], positive=True)
            if row["eta"] is None:
                reason = "eta_unknown"
            elif not row["is_confirmed"]:
                reason = "unconfirmed"
            elif row["eta"] > need_by_date:
                reason = "after_need_by_date"
            elif row["allocated_to"] is None:
                eligible.append(row)
                continue
            elif str(row["allocated_to"]) == str(demand_id):
                committed.append(row)
                continue
            else:
                reason = "allocated_to_other_demand"
            excluded.append({**row, "excluded_reason": reason})
        incoming = sum((r["qty"] for r in eligible), Decimal(0))
        own_incoming = sum((r["qty"] for r in committed), Decimal(0))
        available = quantity(available_stock + incoming)
        unmet = max(Decimal(0), required_qty - own_incoming)
        shortage = quantity(max(Decimal(0), unmet - available))
    warnings = ["advisory_only_no_reservation", "recheck_before_execution"]
    if not inventory:
        warnings.append("inventory_snapshot_missing")
    if allocations:
        warnings.append("resource_competition")
    if other > on_hand:
        warnings.append("allocations_exceed_on_hand")
    return {"required_qty": required_qty, "allocatable_qty": available, "shortage_qty": shortage,
            "breakdown": {"inventory": inventory, "allocations": allocations,
                          "eligible_transit": eligible, "own_committed_transit": committed,
                          "excluded_transit": excluded, "on_hand_qty": on_hand,
                          "other_allocated_qty": other, "available_stock_qty": available_stock,
                          "unallocated_transit_qty": incoming, "own_transit_qty": own_incoming,
                          "unmet_qty": unmet, "warnings": warnings,
                          "quantity_basis": "component units; same as BOM quantity",
                          "quarantine_excluded": True}}


def json_value(value):
    """Preserve exact numbers and evidence identities in JSONB."""
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    from uuid import UUID
    if isinstance(value, UUID):
        return str(value)
    return value


def calculate_shortages(repository, run_id):
    with repository.atomic():
        run = repository.get_run(run_id)
        demand = repository.get_demand(run["demand_id"])
        if run["tenant_id"] != demand["tenant_id"]:
            raise ValueError("Run and demand tenant mismatch")
        lines = repository.demand_lines(demand["demand_id"])
        if not lines:
            raise ValueError("Demand has not been expanded")
        grouped = defaultdict(list)
        unresolved = []
        for line in lines:
            if line["unresolved_reason"] is not None:
                unresolved.append(line)
            else:
                grouped[line["component_id"]].append(line)
        supply = repository.supply_snapshot(demand["tenant_id"])
        snapshots = []
        for component_id, component_lines in sorted(grouped.items()):
            with localcontext() as ctx:
                ctx.prec = 60
                total = quantity(sum((r["required_qty"] for r in component_lines), Decimal(0)),
                                 positive=True)
            args = [[r for r in supply[k] if r["component_id"] == component_id]
                    for k in ("inventory", "allocations", "transit")]
            result = compute_shortage(total, demand["demand_id"], demand["need_by_date"], *args)
            result["breakdown"]["demand_lines"] = component_lines
            result["breakdown"]["is_simulated"] = demand["is_simulated"] or any(
                r.get("is_simulated", False) for group in args for r in group)
            result["breakdown"]["need_by_date"] = demand["need_by_date"]
            snapshot = {"snapshot_id": uuid4(), "run_id": run_id,
                        "component_id": component_id, **result}
            repository.insert_shortage({**snapshot, "breakdown": json_value(result["breakdown"])})
            snapshots.append(snapshot)
        return {"snapshots": snapshots, "unresolved_lines": unresolved,
                "ready": not unresolved and all(
                    "inventory_snapshot_missing" not in s["breakdown"]["warnings"]
                    for s in snapshots)}
