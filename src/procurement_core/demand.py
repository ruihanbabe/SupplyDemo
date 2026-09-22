"""BOM demand expansion; arithmetic is gated by an explicit, versioned policy."""
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from procurement_core.numbers import quantity


@dataclass(frozen=True)
class ExpansionPolicy:
    """What the caller has established about data this BOM only asserts.

    Both flags encode something nobody verified at ingest time: whether the `quantity`
    column means per-board or total, and whether a candidate's identity can be trusted.
    Refusing to compute until each row is individually confirmed would mean never
    computing anything, so the decision is made once, as a versioned business rule, and
    handed in here. The default stays strict: with no policy, nothing expands.

    `versions` carries the rule versions this policy came from, so any number derived
    under it can be traced back to the rule that allowed it.
    """

    quantity_basis_confirmed: bool = False
    accepted_identity_status: frozenset[str] = frozenset({"verified"})
    versions: dict[str, int] = field(default_factory=dict)


#: business_rule ids this policy is built from.
QUANTITY_BASIS_RULE = "bom.quantity_basis"
IDENTITY_POLICY_RULE = "component.identity_policy"


def load_policy(repository, tenant_id="default") -> ExpansionPolicy:
    """Build the policy from versioned business rules, not from code constants."""
    basis = repository.effective_rule(QUANTITY_BASIS_RULE, tenant_id)
    identity = repository.effective_rule(IDENTITY_POLICY_RULE, tenant_id)
    versions = {}
    confirmed = False
    if basis is not None:
        # Only "per_board" licenses the multiplication. "total" or anything unrecognised
        # leaves it blocked rather than guessing which reading was meant.
        confirmed = (basis["value"] or {}).get("basis") == "per_board"
        versions[QUANTITY_BASIS_RULE] = basis["version"]
    accepted = frozenset({"verified"})
    if identity is not None:
        accepted = frozenset((identity["value"] or {}).get("accepted_status") or ["verified"])
        versions[IDENTITY_POLICY_RULE] = identity["version"]
    return ExpansionPolicy(quantity_basis_confirmed=confirmed,
                           accepted_identity_status=accepted, versions=versions)


def expand_lines(production_qty: Decimal, bom_lines: list[dict],
                 selected: dict[str, str],
                 policy: ExpansionPolicy | None = None) -> list[dict]:
    policy = policy or ExpansionPolicy()
    quantity(production_qty, positive=True)
    known = {row["line_id"] for row in bom_lines}
    if set(selected) - known:
        raise ValueError("Selection refers to a line outside this demand BOM")
    result = []
    for row in bom_lines:
        line_id = row["line_id"]
        if not (policy.quantity_basis_confirmed or row["qty_basis_verified"]):
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
        elif candidates[chosen]["identity_status"] not in policy.accepted_identity_status:
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
        policy = load_policy(repository, tenant_id)
        lines = expand_lines(production_qty, bom_lines, selected or {}, policy)
        repository.save_demand_lines(demand_id, lines)
        return {"demand_id": demand_id, "lines": lines,
                "ready": all(row["unresolved_reason"] is None for row in lines),
                "policy_versions": policy.versions,
                "confirmation_requests": confirmation_requests(bom_lines, lines)}


def confirmation_requests(bom_lines, expanded_lines):
    """Keep original manufacturer/MPN suffixes visible when asking for confirmation."""
    by_id = {row["line_id"]: row for row in bom_lines}
    return [{"line_id": line["line_id"], "reason": line["unresolved_reason"],
             "candidates": [dict(c) for c in by_id[line["line_id"]]["candidates"]]}
            for line in expanded_lines if line["unresolved_reason"] is not None]

'''
核心设计思想：
事务原子性：主表写入、BOM 展开、明细保存要么全成功，要么全回滚。
数量校验前置：任何数学运算前后都要调 quantity() 校验，绝不把越界数塞进数据库。
状态机驱动：通过 unresolved_reason 的不同值（None / quantity_basis_unverified / missing_candidate / candidate_selection_required / candidate_identity_unverified）精准标记每一行的卡点，前端据此渲染不同的交互界面。
数据隔离：confirmation_requests 中用 dict(c) 浅拷贝候选数据，避免前端拿到的是内存中的原始对象引用。
'''
