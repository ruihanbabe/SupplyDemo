"""F47 immutable BOM sources, exact requirements and quote validity evidence.

Business arithmetic and time status are deterministic. A Specialist interprets
these records; it cannot choose another BOM, invent prices or execute actions.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from supplyagent.service.domains.business_nodes import FollowupPolicy, validate_case_parts
from supplyagent.service.domains.supply import (
    PostgresSupplyRepository,
    SupplyDemand,
    SupplyFacts,
    calculate_impact,
)
from supplyagent.service.persistence.tenant import TenantAccessError
from supplyagent.service.workflows.specialists import Citation


@dataclass(frozen=True)
class BomLine:
    line_key: str
    manufacturer: str
    mpn: str
    quantity_per_unit: Decimal


@dataclass(frozen=True)
class BomRequirement:
    mpn: str
    manufacturer: str
    quantity: Decimal


def bom_requirements(lines: tuple[BomLine, ...], units: Decimal) -> tuple[BomRequirement, ...]:
    if not units.is_finite() or units <= 0 or not lines:
        raise ValueError("positive units and BOM lines are required")
    totals: dict[str, Decimal] = defaultdict(Decimal)
    manufacturers: dict[str, str] = {}
    keys: set[str] = set()
    with localcontext() as context:
        context.prec = 48  # numeric(18,6) products plus at most 1024 source lines.
        for line in lines:
            if not line.quantity_per_unit.is_finite() or line.quantity_per_unit <= 0:
                raise ValueError("BOM quantity must be positive and exact")
            if line.line_key in keys:
                raise ValueError("duplicate BOM line key")
            keys.add(line.line_key)
            if line.mpn in manufacturers and manufacturers[line.mpn] != line.manufacturer:
                raise ValueError("ambiguous manufacturer for a case part number")
            manufacturers[line.mpn] = line.manufacturer
            totals[line.mpn] += units * line.quantity_per_unit
        return tuple(BomRequirement(p, manufacturers[p], totals[p]) for p in sorted(totals))


def source_citation(kind: str, row: dict) -> Citation:
    # A withdrawal changes evidence state without rewriting the published content.
    payload = json.dumps(
        {
            "contentHash": row["content_hash"],
            "revokedAt": row["revoked_at"].astimezone(UTC).isoformat()
            if row["revoked_at"]
            else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return Citation(
        kind,
        str(row["id"]),
        str(row["revision"]),
        "revision",
        hashlib.sha256(payload.encode()).hexdigest(),
    )


def citation_json(ref: Citation) -> dict[str, str]:
    return {
        "sourceKind": ref.source_kind,
        "sourceId": ref.source_id,
        "version": ref.version,
        "locator": ref.locator,
        "fragmentHash": ref.fragment_hash,
    }


async def source_is_current(connection: AsyncConnection, ref: dict) -> bool:
    tables = {"bom_revision": "bom_revision", "quote_revision": "commercial_quote_revision"}
    table = tables.get(ref.get("sourceKind"))
    if table is None:
        return False
    try:
        source_id = UUID(ref["sourceId"])
    except (ValueError, KeyError):
        return False
    # Table names come only from the fixed internal mapping, never caller text.
    cursor = await connection.execute(f"select * from app.{table} where id=%s", (source_id,))
    row = await cursor.fetchone()
    return row is not None and citation_json(source_citation(ref["sourceKind"], row)) == ref


async def bound_bom(
    connection: AsyncConnection,
    opportunity_id: UUID,
    revision_id: UUID | None,
    binding_version: int | None,
) -> dict | None:
    cursor = await connection.execute(
        "select * from app.opportunity_bom_binding where opportunity_id=%s", (opportunity_id,)
    )
    binding = await cursor.fetchone()
    if revision_id is None:
        if binding is not None:
            raise TenantAccessError("BOM binding changed since admission")
        return None
    if (
        binding is None
        or binding["bom_revision_id"] != revision_id
        or binding["version"] != binding_version
    ):
        raise TenantAccessError("BOM binding changed or outside scope")
    cursor = await connection.execute(
        "select b.*,p.code as product_code,p.name as product_name from app.bom_revision b join app.catalog_product p on p.id=b.product_id where b.id=%s",
        (revision_id,),
    )
    row = await cursor.fetchone()
    if row is None or row["revoked_at"] is not None:
        raise TenantAccessError("bound BOM outside scope or withdrawn")
    lines = tuple(
        BomLine(x["lineKey"], x["manufacturer"], x["mpn"], Decimal(x["quantityPerUnit"]))
        for x in row["lines"]
    )
    requirements = bom_requirements(lines, binding["units"])
    return {
        "bomRevisionId": row["id"],
        "revision": row["revision"],
        "contentHash": row["content_hash"],
        "productCode": row["product_code"],
        "productName": row["product_name"],
        "bindingVersion": binding["version"],
        "units": binding["units"],
        "requiredOn": binding["required_on"],
        "lines": [asdict(line) for line in lines],
        "requirements": [asdict(r) for r in requirements],
        "sourceKind": row["source_kind"],
        "sourceCitations": [citation_json(source_citation("bom_revision", row))],
    }


@dataclass(frozen=True)
class QuoteFact:
    id: UUID
    reference: str
    revision: int
    bom_revision_id: UUID
    quote_kind: str
    bom_line_key: str | None
    quantity: Decimal
    unit_price: Decimal | None
    currency: str | None
    issued_at: datetime
    valid_until: datetime | None
    revoked_at: datetime | None
    estimated_ship_on: date | None = None
    confirmed_arrival_on: date | None = None
    stock_reserved: bool = False


def quote_validity(
    quote: QuoteFact, as_of: datetime, bom_revision_id: UUID, latest_issued_revision: int
) -> str:
    times = (as_of, quote.issued_at, quote.valid_until, quote.revoked_at)
    if any(t is not None and (t.tzinfo is None or t.utcoffset() is None) for t in times):
        raise ValueError("quote timestamps must include timezone")
    if quote.revoked_at is not None:
        return "withdrawn"
    if quote.bom_revision_id != bom_revision_id:
        return "bom_mismatch"
    if quote.issued_at > as_of:
        return "not_yet_valid"
    if quote.revision < latest_issued_revision:
        return "superseded"
    if quote.valid_until is None:
        return "validity_unknown"
    if quote.valid_until <= as_of:
        return "expired"
    return "within_validity_window"


async def quote_context(
    connection: AsyncConnection, opportunity_id: UUID, bom: dict | None, as_of: datetime
) -> dict:
    if bom is None:
        return {
            "bom": None,
            "quotes": [],
            "sourceCitations": [],
            "unknowns": ["当前可见范围未建立版本化 BOM 关联，不能核验报价适用版本。"],
        }
    cursor = await connection.execute(
        "select * from app.commercial_quote_revision where opportunity_id=%s order by reference,revision,id",
        (opportunity_id,),
    )
    rows = await cursor.fetchall()
    latest: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["reference"], row["quote_kind"])
        if row["issued_at"] <= as_of:
            latest[key] = max(latest.get(key, 0), row["revision"])
    output = []
    unknowns = ["修订比较仅针对当前可见报价，不推断不可见修订不存在。"]
    fields = QuoteFact.__dataclass_fields__
    for row in rows:
        fact = QuoteFact(**{k: row[k] for k in fields})
        status = quote_validity(
            fact, as_of, bom["bomRevisionId"], latest.get((fact.reference, fact.quote_kind), 0)
        )
        issues = []
        if fact.unit_price is None:
            issues.append("缺少已记录的价格和币种。")
        if fact.valid_until is None:
            issues.append("缺少报价有效截止时间。")
        if fact.quote_kind == "supplier_offer":
            if not fact.stock_reserved:
                issues.append("尚未锁货；报价数量不等于已确认供应。")
            if fact.confirmed_arrival_on is None:
                issues.append("尚无确认到货日期；预计发货不能代替确认到货。")
        unknowns.extend(issues)
        output.append(
            {**asdict(fact), "validity": status, "sourceKind": row["source_kind"], "issues": issues}
        )
    if not rows:
        unknowns.append("当前可见范围未找到报价，不代表组织内不存在报价。")
    return {
        "bom": bom,
        "asOf": as_of,
        "quotes": output,
        "sourceCitations": bom["sourceCitations"]
        + [citation_json(source_citation("quote_revision", r)) for r in rows],
        "unknowns": list(dict.fromkeys(unknowns)),
    }


async def bom_supply_context(
    connection: AsyncConnection,
    opportunity_id: UUID,
    case_id: UUID,
    as_of: datetime,
    policy: FollowupPolicy,
    parts: tuple[str, ...],
    bom: dict | None,
) -> dict:
    if bom is None:
        return {
            "bom": None,
            "impacts": [],
            "parts": list(parts),
            "scopeNote": "缺少版本化 BOM 关联，未将旧需求行伪装为 BOM 派生需求。",
            "unknowns": ["无法核验版本化 BOM 需求。"],
            "sourceCitations": [],
        }
    if {r["mpn"] for r in bom["requirements"]} != set(parts):
        raise TenantAccessError("BOM and admitted Case part scope differ")
    # Validate the Case scope without loading or calculating obsolete demand rows.
    await validate_case_parts(connection, opportunity_id, case_id, policy, parts)
    repository = PostgresSupplyRepository()
    impacts = []
    for requirement in bom["requirements"]:
        facts = await repository.load_facts(connection, requirement["mpn"])
        demand = SupplyDemand(
            opportunity_id,
            "bound_bom",
            str(bom["bomRevisionId"]),
            requirement["mpn"],
            requirement["mpn"],
            requirement["quantity"],
            bom["requiredOn"],
        )
        scoped = SupplyFacts(facts.cases, facts.inventory, [demand], facts.receipts)
        with localcontext() as context:
            context.prec = 48  # Preserve the exact BOM product through inventory arithmetic.
            impacts.append(
                asdict(
                    calculate_impact(
                        scoped,
                        requirement["mpn"],
                        as_of.astimezone(ZoneInfo(policy.business_timezone)).date(),
                    )
                )
            )
    unknowns = [
        "这是绑定商机的 BOM 覆盖投影，未分配库存，不代表全组织需求可以同时满足或客户排产可承诺。"
    ]
    unknowns.extend(item for impact in impacts for item in impact["unknowns"])
    return {
        "bom": bom,
        "asOf": as_of,
        "parts": list(parts),
        "impacts": impacts,
        "calculationScope": "bound_opportunity_bom",
        "scopeNote": "按固定 BOM 修订与绑定数量计算本商机需求，不重复叠加手工需求行；报价不进入确认收货。",
        "sourceCitations": bom["sourceCitations"],
        "unknowns": unknowns,
    }
