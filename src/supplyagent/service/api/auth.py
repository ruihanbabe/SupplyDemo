"""认证边界：只把已验证的 Supabase 身份交给产品 API。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx


class AuthenticationError(Exception):
    """访问令牌缺失、无效或认证服务尚未配置。"""


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """经过认证边界验证后的最小身份事实。"""

    user_id: UUID
    email: str | None


class AuthVerifier(Protocol):
    """验证 access token，绝不信任请求体中的身份字段。"""

    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        """返回认证服务确认的身份，失败时抛出 AuthenticationError。"""


class UnavailableAuthVerifier:
    """在未配置真实 Supabase 时安全拒绝请求，而不是降级为匿名用户。"""

    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        """不解析未配置环境中的任何 token。"""
        del access_token
        raise AuthenticationError("认证服务未配置")


class SupabaseAuthVerifier:
    """通过 Supabase Auth 的 user endpoint 验证 bearer token。"""

    def __init__(self, auth_url: str, publishable_key: str) -> None:
        """保存服务端配置；publishable key 不是 service role。"""
        self._auth_url = auth_url.rstrip("/")
        self._publishable_key = publishable_key

    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        """把第三方响应当作不可信数据，并只接受有效 UUID 身份。"""
        if not access_token:
            raise AuthenticationError("缺少访问令牌")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self._auth_url}/user",
                    headers={"apikey": self._publishable_key, "authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError as error:
            raise AuthenticationError("认证服务不可用") from error
        if response.status_code != 200:
            raise AuthenticationError("访问令牌无效")
        try:
            payload = response.json()
            user_id = UUID(str(payload["id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise AuthenticationError("认证服务返回无效身份") from error
        email = payload.get("email")
        if email is not None and not isinstance(email, str):
            raise AuthenticationError("认证服务返回无效邮箱")
        return AuthenticatedIdentity(user_id=user_id, email=email)
