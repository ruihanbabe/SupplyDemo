"""F31 的角色能力交集：令牌只能缩小，不能扩大权限。"""

from collections.abc import Iterable

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "organization_admin": frozenset({"organization:read", "crm:read", "crm:propose", "supply:read", "supply:propose", "approval:review", "audit:read", "member:manage"}),
    "sales_manager": frozenset({"organization:read", "crm:read", "crm:propose", "supply:read"}),
    "buyer": frozenset({"organization:read", "supply:read", "supply:propose"}),
    "approver": frozenset({"organization:read", "crm:read", "supply:read", "approval:review"}),
    "hardware_engineer": frozenset({"organization:read", "supply:read", "supply:propose"}),
    "planner": frozenset({"organization:read", "supply:read"}),
    "auditor": frozenset({"organization:read", "crm:read", "supply:read", "audit:read"}),
    "maintainer": frozenset({"organization:read", "audit:read"}),
}


def is_known_role(role: str) -> bool:
    """只有 migration 枚举中的角色可获得能力。"""
    return role in ROLE_CAPABILITIES


def effective_capabilities(role: str, token_capabilities: Iterable[str] | None = None) -> list[str]:
    """返回角色能力，或与短期令牌能力的交集。"""
    role_capabilities = ROLE_CAPABILITIES.get(role)
    if role_capabilities is None:
        return []
    if token_capabilities is None:
        return sorted(role_capabilities)
    return sorted(capability for capability in token_capabilities if capability in role_capabilities)
