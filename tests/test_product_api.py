"""F31 Python API 的离线契约测试。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi.testclient import TestClient

from product_api.app import ORGANIZATION_COOKIE, create_app
from product_api.auth import AuthenticatedIdentity, AuthenticationError
from product_api.rbac import effective_capabilities
from product_api.tenant import OrganizationMembership, TenantAccessError, TenantConfigurationError

ACTOR_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ALPHA_ID = UUID("11111111-1111-1111-1111-111111111111")
BRAVO_ID = UUID("22222222-2222-2222-2222-222222222222")


class FixedAuthVerifier:
    """固定 token 夹具，不连接真实 Supabase。"""

    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        """只承认测试 token，证明路由不接受 body 身份。"""
        if access_token != "fixed-token":
            raise AuthenticationError("访问令牌无效")
        return AuthenticatedIdentity(user_id=ACTOR_ID, email="user@example.test")


class FakeTenantRepository:
    """记录事务调用的内存租户适配器。"""

    def __init__(self) -> None:
        """仅给测试用户提供 Alpha 成员关系。"""
        self.memberships = [
            OrganizationMembership(ALPHA_ID, "alpha-org", "Alpha", "organization_admin", {}),
        ]
        self.context_calls: list[tuple[UUID, UUID]] = []

    async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
        """未知 actor 没有任何可见组织。"""
        return self.memberships if actor_id == ACTOR_ID else []

    async def in_organization(
        self,
        actor_id: UUID,
        organization_id: UUID,
        operation: Callable[[object], Awaitable[OrganizationMembership]],
    ) -> OrganizationMembership:
        """模拟 SQL `set_request_context` 先于租户操作执行。"""
        if actor_id != ACTOR_ID or organization_id != ALPHA_ID:
            raise TenantAccessError("不是该组织成员")
        self.context_calls.append((actor_id, organization_id))
        return await operation(object())

    async def select_organization(
        self, actor_id: UUID, organization_id: UUID
    ) -> OrganizationMembership:
        """模拟同一租户事务中的成员重验。"""
        if actor_id != ACTOR_ID or organization_id != ALPHA_ID:
            raise TenantAccessError("不是该组织成员")
        self.context_calls.append((actor_id, organization_id))
        return self.memberships[0]


def client() -> tuple[TestClient, FakeTenantRepository]:
    """用明确的端口替换真实认证和 PostgreSQL。"""
    repository = FakeTenantRepository()
    return TestClient(create_app(FixedAuthVerifier(), repository)), repository


def test_health_needs_no_authentication_or_database() -> None:
    """健康检查保持可供编排器探测。"""
    api, _ = client()
    response = api.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"status": "ok", "service": "supplyagent-api"}}


def test_session_derives_identity_from_verified_token() -> None:
    """会话不能从请求体、cookie 或浏览器字段取得身份。"""
    api, _ = client()
    response = api.get("/v1/session", headers={"authorization": "Bearer fixed-token"})
    assert response.status_code == 200
    assert response.json()["data"]["user"] == {"userId": str(ACTOR_ID), "email": "user@example.test"}
    assert response.json()["data"]["selectedOrganization"] is None
    assert response.json()["data"]["organizations"][0]["capabilities"] == effective_capabilities(
        "organization_admin"
    )


def test_session_rejects_missing_or_invalid_token() -> None:
    """没有 token 不得退化为匿名会话。"""
    api, _ = client()
    response = api.get("/v1/session")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_organization_selection_rechecks_membership_in_tenant_context() -> None:
    """组织选择必须通过服务端事务，而不是相信客户端 cookie。"""
    api, repository = client()
    response = api.post(
        "/v1/organizations/select",
        headers={"authorization": "Bearer fixed-token"},
        json={"organizationId": str(ALPHA_ID)},
    )
    assert response.status_code == 200
    assert repository.context_calls == [(ACTOR_ID, ALPHA_ID)]
    assert ORGANIZATION_COOKIE in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_organization_selection_rejects_another_organization() -> None:
    """即使已认证也不能选择不属于自己的组织。"""
    api, repository = client()
    response = api.post(
        "/v1/organizations/select",
        headers={"authorization": "Bearer fixed-token"},
        json={"organizationId": str(BRAVO_ID)},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "organization_forbidden"
    assert repository.context_calls == []


def test_database_configuration_failure_is_not_an_empty_session() -> None:
    """数据库未配置应明确报告服务不可用，而不是返回空组织列表。"""

    class UnavailableRepository(FakeTenantRepository):
        """用固定认证器测试 repository 配置失败的 HTTP 契约。"""

        async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
            """不读取任何测试成员关系。"""
            del actor_id
            raise TenantConfigurationError("产品数据库未配置")

    response = TestClient(create_app(FixedAuthVerifier(), UnavailableRepository())).get(
        "/v1/session", headers={"authorization": "Bearer fixed-token"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "product_database_unavailable"


def test_invalid_organization_schema_uses_the_standard_error_envelope() -> None:
    """API 不向 BFF 暴露 FastAPI 默认的 validation detail 格式。"""
    api, _ = client()
    response = api.post(
        "/v1/organizations/select",
        headers={"authorization": "Bearer fixed-token"},
        json={"organizationId": "not-a-uuid"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
