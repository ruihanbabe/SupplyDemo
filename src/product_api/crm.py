"""F36 P3 的 F32 只读 CRM 事实切片。

本模块没有 CRM 写命令、HTTP 路由或 Proposal 行为。调用方必须先经
``TenantRepository.in_organization`` 建立同一事务内的 RLS 上下文。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection

from product_api.specialists import Citation


class CrmFactAccessError(Exception):
    """商机不存在或当前成员没有可读取的 scoped CRM 事实。"""


def _fragment_hash(value: dict[str, object]) -> str:
    """把最小事实投影规范化后指纹化，绝不使用可变整行或联系人邮箱。"""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CrmFactReference:
    """可由后续 Citation 校验器重新读取的 CRM 对象版本投影。"""

    object_type: str
    object_id: UUID
    version: int
    locator: str
    fragment_hash: str

    def citation(self) -> Citation:
        return Citation("crm_record", str(self.object_id), str(self.version), self.locator, self.fragment_hash)


@dataclass(frozen=True)
class CrmCustomerFact:
    id: UUID
    display_name: str
    lifecycle: str
    version: int

    def reference(self) -> CrmFactReference:
        return CrmFactReference(
            "customer", self.id, self.version, "record",
            _fragment_hash({"type": "customer", "id": str(self.id), "displayName": self.display_name,
                            "lifecycle": self.lifecycle, "version": self.version}),
        )


@dataclass(frozen=True)
class CrmOpportunityFact:
    id: UUID
    title: str
    stage: str
    supply_case_id: UUID | None
    version: int

    def reference(self) -> CrmFactReference:
        return CrmFactReference(
            "opportunity", self.id, self.version, "record",
            _fragment_hash({"type": "opportunity", "id": str(self.id), "title": self.title,
                            "stage": self.stage, "supplyCaseId": str(self.supply_case_id) if self.supply_case_id else None,
                            "version": self.version}),
        )


@dataclass(frozen=True)
class CrmContactFact:
    id: UUID
    display_name: str
    version: int

    def reference(self) -> CrmFactReference:
        return CrmFactReference(
            "contact", self.id, self.version, "record",
            _fragment_hash({"type": "contact", "id": str(self.id), "displayName": self.display_name,
                            "version": self.version}),
        )


@dataclass(frozen=True)
class CrmConversationFact:
    id: UUID
    channel: str
    summary: str
    version: int
    updated_at: datetime

    def reference(self) -> CrmFactReference:
        return CrmFactReference(
            "conversation", self.id, self.version, "record",
            _fragment_hash({"type": "conversation", "id": str(self.id), "channel": self.channel,
                            "summary": self.summary, "version": self.version,
                            "updatedAt": self.updated_at.isoformat()}),
        )


@dataclass(frozen=True)
class CrmTaskFact:
    id: UUID
    title: str
    status: str
    version: int

    def reference(self) -> CrmFactReference:
        return CrmFactReference(
            "task", self.id, self.version, "record",
            _fragment_hash({"type": "task", "id": str(self.id), "title": self.title,
                            "status": self.status, "version": self.version}),
        )


@dataclass(frozen=True)
class CrmOpportunityFacts:
    """一个 Mission 允许读取的、最小的客户和商机事实快照。"""

    customer: CrmCustomerFact
    opportunity: CrmOpportunityFact
    contacts: tuple[CrmContactFact, ...]
    conversations: tuple[CrmConversationFact, ...]
    tasks: tuple[CrmTaskFact, ...]

    def references(self) -> tuple[CrmFactReference, ...]:
        return (
            self.customer.reference(), self.opportunity.reference(),
            *(contact.reference() for contact in self.contacts),
            *(conversation.reference() for conversation in self.conversations),
            *(task.reference() for task in self.tasks),
        )


class PostgresCrmFactRepository:
    """只使用 F32 既有表的 RLS 查询；不接受客户端拼接条件。"""

    async def load_opportunity_facts(
        self, connection: AsyncConnection[dict[str, object]], opportunity_id: UUID
    ) -> CrmOpportunityFacts:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                select o.id as opportunity_id, o.title, o.stage, o.supply_case_id,
                       o.version as opportunity_version,
                       c.id as customer_id, c.display_name, c.lifecycle,
                       c.version as customer_version
                from app.crm_opportunity o
                join app.crm_customer c on c.id = o.customer_id
                where o.id = %s
                """,
                (opportunity_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CrmFactAccessError("商机不存在或无 CRM 读取权限")
            customer = CrmCustomerFact(
                row["customer_id"], str(row["display_name"]), str(row["lifecycle"]),
                int(row["customer_version"]),
            )
            opportunity = CrmOpportunityFact(
                row["opportunity_id"], str(row["title"]), str(row["stage"]),
                row["supply_case_id"], int(row["opportunity_version"]),
            )
            await cursor.execute(
                "select id, display_name, version from app.crm_contact where customer_id=%s order by id",
                (customer.id,),
            )
            contacts = tuple(
                CrmContactFact(item["id"], str(item["display_name"]), int(item["version"]))
                for item in await cursor.fetchall()
            )
            await cursor.execute(
                """select id, channel, summary, version, updated_at from app.crm_conversation
                   where opportunity_id=%s order by created_at, id""",
                (opportunity.id,),
            )
            conversations = tuple(
                CrmConversationFact(
                    item["id"], str(item["channel"]), str(item["summary"]),
                    int(item["version"]), item["updated_at"],
                )
                for item in await cursor.fetchall()
            )
            await cursor.execute(
                "select id, title, status, version from app.crm_task where opportunity_id=%s order by created_at, id",
                (opportunity.id,),
            )
            tasks = tuple(
                CrmTaskFact(item["id"], str(item["title"]), str(item["status"]), int(item["version"]))
                for item in await cursor.fetchall()
            )
        return CrmOpportunityFacts(customer, opportunity, contacts, conversations, tasks)
