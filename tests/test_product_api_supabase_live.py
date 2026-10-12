"""F31：经显式授权的真实 Supabase Auth 到 Python API 的端到端验证。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection

from product_api.app import ORGANIZATION_COOKIE, create_app
from product_api.auth import SupabaseAuthVerifier
from product_api.tenant import PostgresTenantRepository


@dataclass(frozen=True)
class LiveF31Settings:
    """真实认证验证所需的本地、未提交配置。"""

    auth_url: str
    publishable_key: str
    email: str
    password: str
    database_url: str


@pytest.fixture
def live_settings() -> LiveF31Settings:
    """只在用户显式运行 live target 时读取测试凭证。"""
    load_dotenv(".env")
    names = [
        "SUPPLYAGENT_SUPABASE_AUTH_URL",
        "SUPPLYAGENT_SUPABASE_PUBLISHABLE_KEY",
        "SUPPLYAGENT_SUPABASE_TEST_EMAIL",
        "SUPPLYAGENT_SUPABASE_TEST_PASSWORD",
        "SUPPLYAGENT_PRODUCT_DATABASE_URL",
    ]
    values = {name: os.getenv(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.fail("缺少真实 F31 验证所需的本地环境变量：" + ", ".join(missing))
    return LiveF31Settings(
        auth_url=str(values["SUPPLYAGENT_SUPABASE_AUTH_URL"]),
        publishable_key=str(values["SUPPLYAGENT_SUPABASE_PUBLISHABLE_KEY"]),
        email=str(values["SUPPLYAGENT_SUPABASE_TEST_EMAIL"]),
        password=str(values["SUPPLYAGENT_SUPABASE_TEST_PASSWORD"]),
        database_url=str(values["SUPPLYAGENT_PRODUCT_DATABASE_URL"]),
    )


@pytest.mark.integration
@pytest.mark.supabase_live
def test_real_supabase_token_is_revalidated_by_python_api_and_scoped_by_rls(
    live_settings: LiveF31Settings,
) -> None:
    """不信任前端身份字段；只接受 Supabase 确认的测试用户。"""
    asyncio.run(_verify_real_auth_flow(live_settings))


async def _verify_real_auth_flow(settings: LiveF31Settings) -> None:
    """登录、创建临时组织、调用 API，并在 finally 中删除本地 fixture。"""
    access_token, actor_id = await _sign_in_test_user(settings)
    organization_id = uuid4()
    foreign_organization_id = uuid4()
    await _insert_fixture(settings.database_url, organization_id, foreign_organization_id, actor_id)
    try:
        app = create_app(
            SupabaseAuthVerifier(settings.auth_url, settings.publishable_key),
            PostgresTenantRepository(settings.database_url),
        )
        headers = {"authorization": f"Bearer {access_token}"}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://supplyagent.test"
        ) as client:
            session = await client.get("/v1/session", headers=headers)
            assert session.status_code == 200
            session_data = session.json()["data"]
            assert session_data["user"]["userId"] == str(actor_id)
            assert session_data["selectedOrganization"] is None
            assert any(
                item["organizationId"] == str(organization_id)
                for item in session_data["organizations"]
            )

            selected = await client.post(
                "/v1/organizations/select",
                headers=headers,
                json={"organizationId": str(organization_id)},
            )
            assert selected.status_code == 200
            assert ORGANIZATION_COOKIE in selected.headers["set-cookie"]
            assert "HttpOnly" in selected.headers["set-cookie"]
            assert "Secure" in selected.headers["set-cookie"]

            selected_session = await client.get("/v1/session", headers=headers)
            assert selected_session.status_code == 200
            assert selected_session.json()["data"]["selectedOrganization"]["organizationId"] == str(
                organization_id
            )

            foreign_selection = await client.post(
                "/v1/organizations/select",
                headers=headers,
                json={"organizationId": str(foreign_organization_id)},
            )
            assert foreign_selection.status_code == 403
            assert foreign_selection.json()["error"]["code"] == "organization_forbidden"
    finally:
        await _delete_fixture(settings.database_url, organization_id, foreign_organization_id)


async def _sign_in_test_user(settings: LiveF31Settings) -> tuple[str, UUID]:
    """只向 Supabase 发送本地测试账号；不得记录 password 或 token。"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{settings.auth_url.rstrip('/')}/token",
                params={"grant_type": "password"},
                headers={"apikey": settings.publishable_key},
                json={"email": settings.email, "password": settings.password},
            )
    except httpx.HTTPError as error:
        pytest.fail(f"Supabase Auth 不可达：{type(error).__name__}")
    if response.status_code != 200:
        pytest.fail(f"Supabase 测试用户登录失败（HTTP {response.status_code}）")
    try:
        payload = response.json()
        return str(payload["access_token"]), UUID(str(payload["user"]["id"]))
    except (KeyError, TypeError, ValueError) as error:
        pytest.fail(f"Supabase 登录响应无效：{type(error).__name__}")


async def _insert_fixture(
    database_url: str, organization_id: UUID, foreign_organization_id: UUID, actor_id: UUID
) -> None:
    """测试用户只加入第一个临时组织，第二个用于跨组织拒绝断言。"""
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.executemany(
                "insert into app.organization (id, slug, display_name) values (%s, %s, %s)",
                [
                    (organization_id, f"live-{organization_id.hex[:12]}", "Live Auth Verification"),
                    (
                        foreign_organization_id,
                        f"foreign-{foreign_organization_id.hex[:12]}",
                        "Foreign Organization",
                    ),
                ],
            )
            await cursor.execute(
                """
                insert into app.organization_member (organization_id, user_id, role)
                values (%s, %s, 'organization_admin')
                """,
                (organization_id, actor_id),
            )
        await connection.commit()


async def _delete_fixture(
    database_url: str, organization_id: UUID, foreign_organization_id: UUID
) -> None:
    """无论验证结果如何都清理临时组织及其成员关系。"""
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "delete from app.organization where id = any(%s)",
                ([organization_id, foreign_organization_id],),
            )
        await connection.commit()
