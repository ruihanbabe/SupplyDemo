"""F31 最小 FastAPI 外壳：健康检查、会话与组织选择。"""

from __future__ import annotations

from dataclasses import asdict
from os import getenv
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AliasChoices, BaseModel, Field

from product_api.auth import (
    AuthenticatedIdentity,
    AuthenticationError,
    AuthVerifier,
    SupabaseAuthVerifier,
    UnavailableAuthVerifier,
)
from product_api.rbac import effective_capabilities, is_known_role
from product_api.tenant import (
    OrganizationMembership,
    PostgresTenantRepository,
    TenantAccessError,
    TenantConfigurationError,
    TenantRepository,
    UnavailableTenantRepository,
)

ORGANIZATION_COOKIE = "supplyagent_organization"


class OrganizationSelection(BaseModel):
    """浏览器可提供的唯一组织选择提示。"""

    organization_id: UUID = Field(validation_alias=AliasChoices("organizationId", "organization_id"))


def create_app(auth_verifier: AuthVerifier, tenant_repository: TenantRepository) -> FastAPI:
    """创建可注入认证与数据库适配器的产品 API。"""
    app = FastAPI(title="SupplyAgent Product API", version="0.5")

    async def identity_from_request(request: Request) -> AuthenticatedIdentity:
        """只接受 bearer token，并由 AuthVerifier 建立身份事实。"""
        authorization = request.headers.get("authorization", "")
        scheme, _, access_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not access_token:
            raise AuthenticationError("缺少访问令牌")
        return await auth_verifier.verify_access_token(access_token)

    async def repository() -> TenantRepository:
        """将闭包中的受控 repository 注入路由。"""
        return tenant_repository

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(_: Request, error: AuthenticationError) -> JSONResponse:
        """所有认证失败使用稳定且不泄漏细节的错误形状。"""
        return _error_response(401, "unauthenticated", str(error))

    @app.exception_handler(TenantAccessError)
    async def tenant_access_error_handler(_: Request, error: TenantAccessError) -> JSONResponse:
        """成员关系校验失败不能被组织选择 cookie 绕过。"""
        return _error_response(403, "organization_forbidden", str(error))

    @app.exception_handler(TenantConfigurationError)
    async def tenant_configuration_error_handler(_: Request, error: TenantConfigurationError) -> JSONResponse:
        """数据库未配置是服务不可用，不得伪造空业务结果。"""
        return _error_response(503, "product_database_unavailable", str(error))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(_: Request, error: RequestValidationError) -> JSONResponse:
        """请求 schema 失败也使用统一错误包络，不泄漏框架默认形状。"""
        return _error_response(422, "validation_error", "请求格式无效", details=error.errors())

    @app.get("/health")
    async def health() -> dict[str, object]:
        """不触碰身份或数据库的进程健康检查。"""
        return {"ok": True, "data": {"status": "ok", "service": "supplyagent-api"}}

    @app.get("/v1/session")
    async def session(
        request: Request,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        """返回认证身份及其成员关系；cookie 只是被重新核验的选择提示。"""
        organizations = await tenants.list_organizations(identity.user_id)
        selected_id = request.cookies.get(ORGANIZATION_COOKIE)
        selected = _find_selected(organizations, selected_id)
        return {
            "ok": True,
            "data": {
                "user": {"userId": str(identity.user_id), "email": identity.email},
                "organizations": [_membership_payload(item) for item in organizations],
                "selectedOrganization": _membership_payload(selected) if selected else None,
            },
        }

    @app.post("/v1/organizations/select")
    async def select_organization(
        selection: OrganizationSelection,
        response: Response,
        identity: AuthenticatedIdentity = Depends(identity_from_request),  # noqa: B008
        tenants: TenantRepository = Depends(repository),  # noqa: B008
    ) -> dict[str, object]:
        """在租户事务中重验成员关系后写入 HttpOnly 组织选择 cookie。"""

        selected = await tenants.select_organization(identity.user_id, selection.organization_id)
        response.set_cookie(
            key=ORGANIZATION_COOKIE,
            value=str(selected.organization_id),
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return {"ok": True, "data": {"selectedOrganization": _membership_payload(selected)}}

    return app


def create_default_app() -> FastAPI:
    """从环境装配默认适配器；缺配置时认证安全失败。"""
    database_url = getenv("SUPPLYAGENT_PRODUCT_DATABASE_URL")
    auth_url = getenv("SUPPLYAGENT_SUPABASE_AUTH_URL")
    publishable_key = getenv("SUPPLYAGENT_SUPABASE_PUBLISHABLE_KEY")
    verifier: AuthVerifier = (
        SupabaseAuthVerifier(auth_url, publishable_key)
        if auth_url and publishable_key
        else UnavailableAuthVerifier()
    )
    repository: TenantRepository = (
        PostgresTenantRepository(database_url) if database_url else UnavailableTenantRepository()
    )
    return create_app(verifier, repository)


def _membership_payload(membership: OrganizationMembership) -> dict[str, object]:
    """把内部领域对象转换为稳定的 camelCase HTTP 数据。"""
    payload = asdict(membership)
    capabilities = effective_capabilities(membership.role) if is_known_role(membership.role) else []
    return {
        "organizationId": str(payload["organization_id"]),
        "slug": payload["slug"],
        "displayName": payload["display_name"],
        "role": payload["role"],
        "capabilities": capabilities,
        "scope": payload["scope"],
    }


def _find_selected(
    organizations: list[OrganizationMembership], selected_id: str | None
) -> OrganizationMembership | None:
    """仅从认证用户已经枚举出的成员关系中解析 cookie 选择。"""
    return next((item for item in organizations if str(item.organization_id) == selected_id), None)


def _error_response(
    status_code: int, code: str, message: str, details: object | None = None
) -> JSONResponse:
    """提供 API 全局一致的错误包络。"""
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"ok": False, "error": error})
'''
1. 用户注册登录，拿到专属 token（Supabase 签发）
2. 前端调 GET /v1/session
   → 后端验 token 得 identity，查已核验组织列表，读 cookie 对质
   → selectedOrganization: null
3. 前端看到 null → 弹出"选择组织"界面
4. 用户选了组织A → 前端发 POST /v1/organizations/select
   headers: Authorization: Bearer <token>     ← 他是谁（已核验事实的来源）
   body: {"organizationId": "A的id"}          ← 他想要哪（待核验愿望）
5. FastAPI 注入四件套：
   identity  ← 先验后注入（Depends：问 Supabase）
   tenants   ← 纯递入（Depends：仓库对象，不验证）
   selection ← body 解析+校验
   response  ← 白给的响应编辑器
6. 函数体：tenants.select_organization(张三id, A的id)
   → 开事务 → set local role supplyagent_api → 登记 RLS 上下文
   → 库里查"张三×A"成员关系
   ├─ 不是成员 → TenantAccessError → 403，cookie 不种
   └─ 是成员 → 返回卡片 selected
7. response.set_cookie("supplyagent_organization", A的id, 四道护栏, 12小时)
8. 返回 {"ok":true, "data":{"selectedOrganization": 驼峰卡片}}
9. 浏览器：存 cookie（JS 读不到）+ 更新界面
10. 之后再调 session，第2步的 null 变成 A 的卡片 → 闭环


'''
