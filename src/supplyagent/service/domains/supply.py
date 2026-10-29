"""F39 的组织范围供应链事实、受限命令和确定性影响计算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from psycopg import AsyncConnection


class SupplyAccessError(Exception):
    """当前组织成员没有供应链事实录入能力。"""


@dataclass(frozen=True)
class SupplyCase:
    """供应异常 Case 的最小产品表示。"""

    id: UUID
    title: str
    status: str
    severity: str
    scope: dict[str, object]
    opened_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InventoryPosition:
    """一个料号、库位的库存事实；None 是未知而不是零。"""

    id: UUID
    part_number: str
    location_code: str
    on_hand_qty: Decimal | None
    reserved_qty: Decimal | None
    in_transit_qty: Decimal | None
    verified_at: datetime | None


@dataclass(frozen=True)
class SupplyDemand:
    """一个需求来源行。"""

    id: UUID
    source_type: str
    source_reference: str
    line_key: str
    part_number: str
    required_qty: Decimal | None
    required_on: date | None


@dataclass(frozen=True)
class SupplyReceipt:
    """一笔到货预期；只有在途到货可进入未来投影。"""

    id: UUID
    receipt_reference: str
    line_key: str
    part_number: str
    expected_on: date | None
    quantity: Decimal | None
    status: str


@dataclass(frozen=True)
class SupplyFacts:
    """同一料号的完整、已隔离事实切片。"""

    cases: list[SupplyCase]
    inventory: list[InventoryPosition]
    demands: list[SupplyDemand]
    receipts: list[SupplyReceipt]


@dataclass(frozen=True)
class TimelineEvent:
    """影响计算向 API 公开的一个确定性事件。"""

    on: date
    kind: str
    reference: str
    quantity: Decimal
    projected_available_qty: Decimal


@dataclass(frozen=True)
class SupplyImpact:
    """一条料号的投影结果；未知输入不会伪装成精确预测。"""

    part_number: str
    as_of: date
    opening_available_qty: Decimal | None
    projected_available_qty: Decimal | None
    first_shortage_on: date | None
    timeline: list[TimelineEvent]
    unknowns: list[str]


class SupplyRepository(Protocol):
    """仅在 TenantRepository 已建立的数据库事务中执行供应链操作。"""

    async def list_cases(
        self, connection: AsyncConnection[dict[str, object]]
    ) -> list[SupplyCase]: ...

    async def load_facts(
        self, connection: AsyncConnection[dict[str, object]], part_number: str
    ) -> SupplyFacts: ...

    async def create_case(
        self,
        connection: AsyncConnection[dict[str, object]],
        title: str,
        status: str,
        severity: str,
        scope: dict[str, object],
    ) -> UUID: ...

    async def upsert_inventory(
        self,
        connection: AsyncConnection[dict[str, object]],
        part_number: str,
        location_code: str,
        on_hand_qty: Decimal | None,
        reserved_qty: Decimal | None,
        in_transit_qty: Decimal | None,
        verified_at: datetime | None,
    ) -> UUID: ...

    async def upsert_demand(
        self,
        connection: AsyncConnection[dict[str, object]],
        source_type: str,
        source_reference: str,
        line_key: str,
        part_number: str,
        required_qty: Decimal | None,
        required_on: date | None,
    ) -> UUID: ...

    async def upsert_receipt(
        self,
        connection: AsyncConnection[dict[str, object]],
        receipt_reference: str,
        line_key: str,
        part_number: str,
        expected_on: date | None,
        quantity: Decimal | None,
        status: str,
    ) -> UUID: ...


class UnavailableSupplyRepository:
    """与未配置的 tenant repository 相同，拒绝伪造领域结果。"""

    async def list_cases(self, connection: AsyncConnection[dict[str, object]]) -> list[SupplyCase]:
        del connection
        raise SupplyAccessError("供应链服务未配置")

    async def load_facts(
        self, connection: AsyncConnection[dict[str, object]], part_number: str
    ) -> SupplyFacts:
        del connection, part_number
        raise SupplyAccessError("供应链服务未配置")

    async def create_case(self, connection: AsyncConnection[dict[str, object]], **_: Any) -> UUID:
        del connection
        raise SupplyAccessError("供应链服务未配置")

    async def upsert_inventory(
        self, connection: AsyncConnection[dict[str, object]], **_: Any
    ) -> UUID:
        del connection
        raise SupplyAccessError("供应链服务未配置")

    async def upsert_demand(self, connection: AsyncConnection[dict[str, object]], **_: Any) -> UUID:
        del connection
        raise SupplyAccessError("供应链服务未配置")

    async def upsert_receipt(
        self, connection: AsyncConnection[dict[str, object]], **_: Any
    ) -> UUID:
        del connection
        raise SupplyAccessError("供应链服务未配置")


class PostgresSupplyRepository:
    """F39 SQL 适配器；普通事实写入依赖 RLS，而非隐式函数 owner 权限。"""

    async def list_cases(self, connection: AsyncConnection[dict[str, object]]) -> list[SupplyCase]:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """select id, title, status, severity, scope, opened_at, updated_at
                   from app.supply_case order by updated_at desc, id"""
            )
            rows = await cursor.fetchall()
        return [self._case(row) for row in rows]

    async def load_facts(
        self, connection: AsyncConnection[dict[str, object]], part_number: str
    ) -> SupplyFacts:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """select id, part_number, location_code, on_hand_qty, reserved_qty,
                          in_transit_qty, verified_at
                   from app.inventory_position where part_number = %s order by location_code""",
                (part_number,),
            )
            inventory = [self._inventory(row) for row in await cursor.fetchall()]
            await cursor.execute(
                """select id, source_type, source_reference, line_key, part_number, required_qty, required_on
                   from app.supply_demand where part_number = %s order by required_on nulls last, id""",
                (part_number,),
            )
            demands = [self._demand(row) for row in await cursor.fetchall()]
            await cursor.execute(
                """select id, receipt_reference, line_key, part_number, expected_on, quantity, status
                   from app.supply_receipt where part_number = %s order by expected_on nulls last, id""",
                (part_number,),
            )
            receipts = [self._receipt(row) for row in await cursor.fetchall()]
        return SupplyFacts(cases=[], inventory=inventory, demands=demands, receipts=receipts)

    async def create_case(
        self, connection: AsyncConnection[dict[str, object]], **values: Any
    ) -> UUID:
        return await self._write(
            connection,
            """insert into app.supply_case (organization_id, title, status, severity, scope)
               values (app.current_organization_id(), %s, %s, %s, %s::jsonb) returning id""",
            (values["title"], values["status"], values["severity"], _json(values["scope"])),
        )

    async def upsert_inventory(
        self, connection: AsyncConnection[dict[str, object]], **values: Any
    ) -> UUID:
        return await self._write(
            connection,
            """insert into app.inventory_position
                 (organization_id, part_number, location_code, on_hand_qty, reserved_qty, in_transit_qty, verified_at)
               values (app.current_organization_id(), %s, %s, %s, %s, %s, %s)
               on conflict (organization_id, part_number, location_code) do update set
                 on_hand_qty = excluded.on_hand_qty, reserved_qty = excluded.reserved_qty,
                 in_transit_qty = excluded.in_transit_qty, verified_at = excluded.verified_at, updated_at = now()
               where excluded.verified_at >= app.inventory_position.verified_at
               returning id""",
            tuple(
                values[name]
                for name in (
                    "part_number",
                    "location_code",
                    "on_hand_qty",
                    "reserved_qty",
                    "in_transit_qty",
                    "verified_at",
                )
            ),
            allow_stale_inventory=True,
        )

    async def upsert_demand(
        self, connection: AsyncConnection[dict[str, object]], **values: Any
    ) -> UUID:
        return await self._write(
            connection,
            """insert into app.supply_demand
                 (organization_id, source_type, source_reference, line_key, part_number, required_qty, required_on)
               values (app.current_organization_id(), %s, %s, %s, %s, %s, %s)
               on conflict (organization_id, source_type, source_reference, line_key) do update set
                 part_number = excluded.part_number, required_qty = excluded.required_qty,
                 required_on = excluded.required_on, updated_at = now()
               returning id""",
            tuple(
                values[name]
                for name in (
                    "source_type",
                    "source_reference",
                    "line_key",
                    "part_number",
                    "required_qty",
                    "required_on",
                )
            ),
        )

    async def upsert_receipt(
        self, connection: AsyncConnection[dict[str, object]], **values: Any
    ) -> UUID:
        return await self._write(
            connection,
            """insert into app.supply_receipt
                 (organization_id, receipt_reference, line_key, part_number, expected_on, quantity, status)
               values (app.current_organization_id(), %s, %s, %s, %s, %s, %s)
               on conflict (organization_id, receipt_reference, line_key) do update set
                 part_number = excluded.part_number, expected_on = excluded.expected_on,
                 quantity = excluded.quantity, status = excluded.status, updated_at = now()
               returning id""",
            tuple(
                values[name]
                for name in (
                    "receipt_reference",
                    "line_key",
                    "part_number",
                    "expected_on",
                    "quantity",
                    "status",
                )
            ),
        )

    async def _write(
        self,
        connection: AsyncConnection[dict[str, object]],
        query: str,
        values: tuple[object, ...],
        allow_stale_inventory: bool = False,
    ) -> UUID:
        """执行参数化 DML；RLS 是成员级写入的数据库最终裁判。"""
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(query, values)
                row = await cursor.fetchone()
        except Exception as error:
            if getattr(error, "sqlstate", None) == "42501":
                raise SupplyAccessError("不是当前组织成员，不能录入供应链事实") from error
            raise
        if row is None:
            # 只有旧库存快照会触发 no-op；读取既有 ID 能让调用者安全重试。
            if allow_stale_inventory:
                return await self._inventory_id_after_stale_write(connection, values)
            raise RuntimeError("供应链写入未返回标识")
        return UUID(str(row["id"]))

    async def _inventory_id_after_stale_write(
        self, connection: AsyncConnection[dict[str, object]], values: tuple[object, ...]
    ) -> UUID:
        """仅库存 upsert 可因观测时间较旧不更新；其他 DML 必须返回一行。"""
        if len(values) != 6:
            raise RuntimeError("供应链写入未返回标识")
        async with connection.cursor() as cursor:
            await cursor.execute(
                """select id from app.inventory_position
                   where organization_id = app.current_organization_id()
                     and part_number = %s and location_code = %s""",
                (values[0], values[1]),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("较旧库存快照未找到既有事实")
        return UUID(str(row["id"]))

    @staticmethod
    def _case(row: dict[str, object]) -> SupplyCase:
        scope = row["scope"]
        if not isinstance(scope, dict):
            raise TypeError("supply_case.scope 必须是对象")
        return SupplyCase(
            UUID(str(row["id"])),
            str(row["title"]),
            str(row["status"]),
            str(row["severity"]),
            scope,
            row["opened_at"],
            row["updated_at"],
        )

    @staticmethod
    def _inventory(row: dict[str, object]) -> InventoryPosition:
        return InventoryPosition(
            UUID(str(row["id"])),
            str(row["part_number"]),
            str(row["location_code"]),
            row["on_hand_qty"],
            row["reserved_qty"],
            row["in_transit_qty"],
            row["verified_at"],
        )

    @staticmethod
    def _demand(row: dict[str, object]) -> SupplyDemand:
        return SupplyDemand(
            UUID(str(row["id"])),
            str(row["source_type"]),
            str(row["source_reference"]),
            str(row["line_key"]),
            str(row["part_number"]),
            row["required_qty"],
            row["required_on"],
        )

    @staticmethod
    def _receipt(row: dict[str, object]) -> SupplyReceipt:
        return SupplyReceipt(
            UUID(str(row["id"])),
            str(row["receipt_reference"]),
            str(row["line_key"]),
            str(row["part_number"]),
            row["expected_on"],
            row["quantity"],
            str(row["status"]),
        )


def calculate_impact(facts: SupplyFacts, part_number: str, as_of: date) -> SupplyImpact:
    """以固定排序投影库存；任一关键未知值都会阻止虚假的精确余额。"""
    unknowns: list[str] = []
    opening_values: list[Decimal] = []
    for position in facts.inventory:
        if position.on_hand_qty is None or position.reserved_qty is None:
            unknowns.append(f"inventory:{position.location_code}")
        elif position.in_transit_qty is None:
            unknowns.append(f"inventory-in-transit:{position.location_code}")
        elif position.in_transit_qty != 0:
            # 聚合在途量没有与 receipt_reference 的关联，不能与逐笔 receipt 重复计入。
            unknowns.append(f"inventory-in-transit-unreconciled:{position.location_code}")
        else:
            opening_values.append(position.on_hand_qty - position.reserved_qty)
    opening = sum(opening_values, Decimal()) if facts.inventory and not unknowns else None
    dated_receipts: list[SupplyReceipt] = []
    dated_demands: list[SupplyDemand] = []
    for receipt in facts.receipts:
        if receipt.status == "in_transit":
            if receipt.expected_on is None or receipt.quantity is None:
                unknowns.append(f"receipt:{receipt.receipt_reference}")
            else:
                dated_receipts.append(receipt)
    for demand in facts.demands:
        if demand.required_on is None or demand.required_qty is None:
            unknowns.append(f"demand:{demand.source_reference}:{demand.line_key}")
        else:
            dated_demands.append(demand)
    if opening is None:
        return SupplyImpact(part_number, as_of, None, None, None, [], unknowns)
    events: list[tuple[date, int, str, str, Decimal]] = []
    for receipt in dated_receipts:
        if receipt.expected_on >= as_of:
            events.append(
                (receipt.expected_on, 0, "receipt", receipt.receipt_reference, receipt.quantity)
            )
    for demand in dated_demands:
        if demand.required_on >= as_of:
            events.append(
                (
                    demand.required_on,
                    1,
                    "demand",
                    f"{demand.source_reference}:{demand.line_key}",
                    -demand.required_qty,
                )
            )
    available = opening
    timeline: list[TimelineEvent] = []
    first_shortage: date | None = None
    for on, _, kind, reference, change in sorted(events):
        available += change
        timeline.append(TimelineEvent(on, kind, reference, abs(change), available))
        if available < 0 and first_shortage is None:
            first_shortage = on
    return SupplyImpact(part_number, as_of, opening, available, first_shortage, timeline, unknowns)


def _json(value: dict[str, object]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), sort_keys=True)
