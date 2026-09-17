"""Versioned advisory plans; approval and external execution are deliberately separate."""
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from procurement_core.numbers import quantity
from procurement_core.offers import recommend
from procurement_core.shortage import json_value


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    if isinstance(value, Decimal):
        quantity(value)
        rendered = format(value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Hash timestamps must include timezone")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, float):
        raise TypeError("Floating-point plan values are forbidden")
    return json_value(value)


def content_hash(payload):
    data = json.dumps(canonical(payload), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def build_plan_content(demand, demand_lines, snapshots, offers):
    """Build a frozen payload from persisted snapshots and explicitly chosen offers."""
    resolved = {}
    unresolved = []
    for line in demand_lines:
        if line["unresolved_reason"] is not None:
            unresolved.append(line)
        else:
            with localcontext() as ctx:
                ctx.prec = 60
                component = line["component_id"]
                resolved[component] = quantity(resolved.get(component, Decimal(0)) +
                                               quantity(line["required_qty"], positive=True))
    if not resolved:
        raise ValueError("No resolved components; confirm BOM inputs before generating a plan")
    indexed = {snapshot["component_id"]: snapshot for snapshot in snapshots}
    if len(indexed) != len(snapshots) or set(indexed) != set(resolved):
        raise ValueError("Exactly one shortage snapshot is required per resolved component")
    if set(offers) - set(resolved):
        raise ValueError("Offer does not belong to this demand")
    context = {"demand": demand, "unresolved_lines": unresolved,
               "snapshot_ids": sorted(str(s["snapshot_id"]) for s in snapshots)}
    lines, complete, distributors = [], not unresolved, set()
    for component, required in sorted(resolved.items()):
        snapshot = indexed[component]
        if snapshot["required_qty"] != required:
            raise ValueError("Shortage snapshot does not match expanded demand quantity")
        shortage = quantity(snapshot["shortage_qty"])
        offer = offers.get(component)
        if offer is not None and offer.component_id != component:
            raise ValueError("Offer component identity mismatch")
        if shortage and offer is None:
            raise ValueError("Positive shortage requires an explicit offer with MOQ/order multiple")
        advice = recommend(shortage, offer) if offer else {
            "suggested_qty": Decimal(0), "unit_price": None, "currency": None,
            "price_break_qty": None, "known_goods_amount": None, "offer": None,
            "warnings": ["no_purchase_required"], "complete_quote": True}
        if offer and shortage:
            distributors.add(offer.distributor)
        complete = complete and advice["complete_quote"] and not any(
            warning in snapshot["breakdown"].get("warnings", [])
            for warning in ("inventory_snapshot_missing", "allocations_exceed_on_hand"))
        lines.append({"component_id": component, "shortage_qty": shortage,
                      "suggested_qty": advice["suggested_qty"],
                      "distributor": offer.distributor if offer else None,
                      "distributor_sku": offer.distributor_sku if offer else None,
                      "unit_price": advice["unit_price"], "currency": advice["currency"],
                      "price_break_qty": advice["price_break_qty"],
                      "moq": offer.moq if offer else None,
                      "order_multiple": offer.order_multiple if offer else None,
                      "lead_time_days": offer.lead_time_days if offer else None,
                      "quote_retrieved_at": offer.retrieved_at if offer else None,
                      "evidence_ref": {"kind": "plan_inputs", "plan_context": context,
                                       "shortage_snapshot": snapshot, "offer": advice["offer"],
                                       "known_goods_amount": advice["known_goods_amount"],
                                       "warnings": advice["warnings"]}})
    single = len(distributors) == 1
    # Persist review readiness with the frozen inputs, never as Run state/approval.
    for line in lines:
        line["evidence_ref"]["ready_for_review"] = bool(complete)
        line["evidence_ref"]["single_source"] = single
    return {"hash_version": 1, "demand_id": demand["demand_id"],
            "tenant_id": demand["tenant_id"], "is_single_source": single, "lines": lines}


def generate_plan(repository, *, run_id, snapshot_ids, offers):
    with repository.atomic():
        run = repository.get_run(run_id)
        # Serialize version allocation on the stable parent row, including first version.
        demand = repository.get_demand(run["demand_id"])
        if run["tenant_id"] != demand["tenant_id"]:
            raise ValueError("Run and demand tenant mismatch")
        lines = repository.demand_lines(demand["demand_id"])
        snapshots = repository.shortage_snapshots(run_id, snapshot_ids)
        payload = build_plan_content(demand, lines, snapshots, offers)
        plan = {"plan_id": uuid4(), "tenant_id": demand["tenant_id"],
                "demand_id": demand["demand_id"],
                "version": repository.next_plan_version(demand["demand_id"]),
                "content_hash": content_hash(payload),
                "is_single_source": payload["is_single_source"]}
        # Store the same canonical representation that was hashed, enabling exact readback.
        frozen = canonical(payload)
        repository.insert_plan(plan, frozen["lines"])
        return {**plan, "lines": frozen["lines"],
                "ready_for_review": frozen["lines"][0]["evidence_ref"]["ready_for_review"]}
