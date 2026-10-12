"""F31 的 PostgreSQL 租户事务边界。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row


class TenantAccessError(Exception):
    """已认证用户无权进入所选组织。"""


class TenantConfigurationError(Exception):
    """产品 API 未取得受限数据库连接配置。"""


@dataclass(frozen=True)
class OrganizationMembership:
    """组织选择和会话展示所需的最小成员关系。"""

    organization_id: UUID
    slug: str
    display_name: str
    role: str
    scope: dict[str, object]


Result = TypeVar("Result")


class TenantRepository(Protocol):
    """组织枚举与单个受 RLS 保护事务的最小接口。"""

    async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
        """列出认证用户的组织，不接受浏览器伪造的成员关系。"""

    async def in_organization(
        self,
        actor_id: UUID,
        organization_id: UUID,
        operation: Callable[[AsyncConnection[dict[str, object]]], Awaitable[Result]],
    ) -> Result:
        """核验成员关系、设置事务局部 RLS 上下文并执行操作。"""

    async def select_organization(
        self, actor_id: UUID, organization_id: UUID
    ) -> OrganizationMembership:
        """在同一 RLS 事务内核验并读取所选组织成员关系。"""


class UnavailableTenantRepository:
    """数据库未配置时拒绝业务读取，但不妨碍健康检查。"""

    async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
        """不使用 actor，避免错误地返回匿名数据。"""
        del actor_id
        raise TenantConfigurationError("产品数据库未配置")

    async def in_organization(
        self,
        actor_id: UUID,
        organization_id: UUID,
        operation: Callable[[AsyncConnection[dict[str, object]]], Awaitable[Result]],
    ) -> Result:
        """不运行任何业务操作。"""
        del actor_id, organization_id, operation
        raise TenantConfigurationError("产品数据库未配置")

    async def select_organization(
        self, actor_id: UUID, organization_id: UUID
    ) -> OrganizationMembership:
        """不返回任何伪造的组织成员关系。"""
        del actor_id, organization_id
        raise TenantConfigurationError("产品数据库未配置")


class PostgresTenantRepository:
    """唯一持有 PostgreSQL 连接字符串的 F31 产品适配器。"""

    def __init__(self, database_url: str) -> None:
        """保留受限 supplyagent_api 连接的 URL。"""
        self._database_url = database_url

    async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
        """调用受限 SQL 函数列出认证用户自己的成员关系。"""
        async with (
            await AsyncConnection.connect(self._database_url, row_factory=dict_row) as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            await cursor.execute("set local role supplyagent_api")
            await cursor.execute("select * from app.list_organizations(%s::uuid)", (actor_id,))
            rows = await cursor.fetchall()
        return [self._membership(row) for row in rows]

    async def in_organization(
        self,
        actor_id: UUID,
        organization_id: UUID,
        operation: Callable[[AsyncConnection[dict[str, object]]], Awaitable[Result]],
    ) -> Result:
        """只在同一个数据库事务中建立并使用组织上下文。"""
        try:
            async with (
                await AsyncConnection.connect(self._database_url, row_factory=dict_row) as connection,
                connection.transaction(),
                connection.cursor() as cursor,
            ):
                await cursor.execute("set local role supplyagent_api")
                await cursor.execute(
                    "select app.set_request_context(%s::uuid, %s::uuid)",
                    (actor_id, organization_id),
                )
                return await operation(connection)
        except Exception as error:
            if getattr(error, "sqlstate", None) == "42501":
                raise TenantAccessError("不是该组织成员") from error
            raise

    async def select_organization(
        self, actor_id: UUID, organization_id: UUID
    ) -> OrganizationMembership:
        """同一事务中以 RLS 上下文读取成员关系，避免跨事务 TOCTOU。"""

        async def read_membership(
            connection: AsyncConnection[dict[str, object]],
        ) -> OrganizationMembership:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    select o.id as organization_id, o.slug, o.display_name, m.role, m.scope
                    from app.organization_member m
                    join app.organization o on o.id = m.organization_id
                    limit 1
                    """
                )
                row = await cursor.fetchone()
            if row is None:
                raise TenantAccessError("不是该组织成员")
            return self._membership(row)

        return await self.in_organization(actor_id, organization_id, read_membership)

    @staticmethod
    def _membership(row: dict[str, object]) -> OrganizationMembership:
        """把 SQL 行转换为固定领域值，拒绝意外的 scope 形状。"""
        scope = row["scope"]
        if not isinstance(scope, dict):
            raise TypeError("organization_member.scope 必须是对象")
        return OrganizationMembership(
            organization_id=UUID(str(row["organization_id"])),
            slug=str(row["slug"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            scope=scope,
        )
