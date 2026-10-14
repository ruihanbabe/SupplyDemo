"""F39 的离线 API 契约和确定性时间线验证。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity, AuthenticationError
from product_api.supply import (
    InventoryPosition,
    SupplyAccessError,
    SupplyDemand,
    SupplyFacts,
    SupplyReceipt,
    calculate_impact,
)
from product_api.tenant import OrganizationMembership, TenantAccessError

ACTOR_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ALPHA_ID = UUID("11111111-1111-1111-1111-111111111111")
BRAVO_ID = UUID("22222222-2222-2222-2222-222222222222")
ITEM_ID = UUID("33333333-3333-3333-3333-333333333333")


class FixedAuth:
    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        if access_token != "fixed-token":
            raise AuthenticationError("访问令牌无效")
        return AuthenticatedIdentity(ACTOR_ID, "buyer@example.test")


class FakeTenantRepository:
    def __init__(self, role: str = "buyer") -> None:
        self._membership = OrganizationMembership(ALPHA_ID, "alpha", "Alpha", role, {})

    async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
        del actor_id
        return [self._membership]

    async def select_organization(
        self, actor_id: UUID, organization_id: UUID
    ) -> OrganizationMembership:
        del actor_id, organization_id
        raise AssertionError("not used")

    async def in_organization(self, actor_id: UUID, organization_id: UUID, operation):
        if actor_id != ACTOR_ID or organization_id != ALPHA_ID:
            raise TenantAccessError("不是该组织成员")
        return await operation(object())

    async def in_organization_as_member(self, actor_id: UUID, organization_id: UUID, operation):
        if actor_id != ACTOR_ID or organization_id != ALPHA_ID:
            raise TenantAccessError("不是该组织成员")
        return await operation(object(), self._membership)


class FakeSupplyRepository:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def list_cases(self, connection):
        del connection
        return []

    async def load_facts(self, connection, part_number: str) -> SupplyFacts:
        del connection
        return _known_facts(part_number)

    async def create_case(self, connection, **values: object) -> UUID:
        del connection
        self.created.append(values)
        return ITEM_ID

    async def upsert_inventory(self, connection, **values: object) -> UUID:
        del connection
        self.created.append(values)
        return ITEM_ID

    async def upsert_demand(self, connection, **values: object) -> UUID:
        del connection
        self.created.append(values)
        return ITEM_ID

    async def upsert_receipt(self, connection, **values: object) -> UUID:
        del connection
        self.created.append(values)
        return ITEM_ID


def _known_facts(part_number: str) -> SupplyFacts:
    return SupplyFacts(
        cases=[],
        inventory=[
            InventoryPosition(
                ITEM_ID, part_number, "MAIN", Decimal(10), Decimal(2), Decimal(0), None
            )
        ],
        demands=[
            SupplyDemand(
                ITEM_ID, "manual", "order-1", "1", part_number, Decimal(12), date(2026, 10, 13)
            )
        ],
        receipts=[
            SupplyReceipt(
                ITEM_ID,
                "shipment-1",
                "1",
                part_number,
                date(2026, 10, 14),
                Decimal(4),
                "in_transit",
            )
        ],
    )


def _client(role: str = "buyer") -> tuple[TestClient, FakeSupplyRepository]:
    supplies = FakeSupplyRepository()
    return TestClient(create_app(FixedAuth(), FakeTenantRepository(role), supplies)), supplies


def test_impact_projects_events_in_fixed_date_order() -> None:
    impact = calculate_impact(_known_facts("PART-A"), "PART-A", date(2026, 10, 12))
    assert impact.opening_available_qty == Decimal(8)
    assert impact.first_shortage_on == date(2026, 10, 13)
    assert [item.projected_available_qty for item in impact.timeline] == [Decimal(-4), Decimal(0)]


def test_impact_does_not_guess_unknown_inventory_or_demand() -> None:
    facts = _known_facts("PART-A")
    unknown = SupplyFacts(
        cases=[],
        inventory=[
            InventoryPosition(ITEM_ID, "PART-A", "MAIN", None, Decimal(2), Decimal(0), None)
        ],
        demands=facts.demands,
        receipts=facts.receipts,
    )
    impact = calculate_impact(unknown, "PART-A", date(2026, 10, 12))
    assert impact.opening_available_qty is None
    assert impact.projected_available_qty is None
    assert impact.first_shortage_on is None
    assert impact.unknowns == ["inventory:MAIN"]


def test_impact_does_not_double_count_an_unreconciled_in_transit_total() -> None:
    facts = _known_facts("PART-A")
    unreconciled = SupplyFacts(
        cases=[],
        inventory=[
            InventoryPosition(ITEM_ID, "PART-A", "MAIN", Decimal(10), Decimal(2), Decimal(3), None)
        ],
        demands=facts.demands,
        receipts=facts.receipts,
    )
    impact = calculate_impact(unreconciled, "PART-A", date(2026, 10, 12))
    assert impact.opening_available_qty is None
    assert impact.unknowns == ["inventory-in-transit-unreconciled:MAIN"]


def test_impact_http_revalidates_org_and_preserves_decimal_precision() -> None:
    api, _ = _client()
    response = api.get(
        "/v1/supply/impact",
        params={"organization_id": str(ALPHA_ID), "part_number": "PART-A", "as_of": "2026-10-12"},
        headers={"authorization": "Bearer fixed-token"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["openingAvailableQty"] == "8"
    denied = api.get(
        "/v1/supply/impact",
        params={"organization_id": str(BRAVO_ID), "part_number": "PART-A"},
        headers={"authorization": "Bearer fixed-token"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "organization_forbidden"


def test_fact_command_requires_database_capability_and_never_accepts_body_actor() -> None:
    api, supplies = _client()
    response = api.put(
        "/v1/supply/facts/inventory",
        headers={"authorization": "Bearer fixed-token"},
        json={
            "organizationId": str(ALPHA_ID),
            "partNumber": "PART-A",
            "locationCode": "MAIN",
            "onHandQty": "1.2500",
            "reservedQty": "0",
            "inTransitQty": None,
            "verifiedAt": "2026-10-12T00:00:00Z",
            "actorId": str(BRAVO_ID),
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["inventoryPositionId"] == str(ITEM_ID)
    assert supplies.created == [
        {
            "part_number": "PART-A",
            "location_code": "MAIN",
            "on_hand_qty": Decimal("1.2500"),
            "reserved_qty": Decimal(0),
            "in_transit_qty": None,
            "verified_at": datetime(2026, 10, 12, tzinfo=UTC),
        }
    ]


def test_supply_writer_role_can_mutate_all_supply_fact_types() -> None:
    """buyer 的 supply:propose 能力覆盖 F39 的四类人工事实命令。"""
    api, supplies = _client("buyer")
    headers = {"authorization": "Bearer fixed-token"}
    organization_id = str(ALPHA_ID)
    commands = [
        (
            "post",
            "/v1/supply/cases",
            {
                "organizationId": organization_id,
                "title": "Case",
                "status": "open",
                "severity": "low",
            },
        ),
        (
            "put",
            "/v1/supply/facts/inventory",
            {
                "organizationId": organization_id,
                "partNumber": "PART-A",
                "locationCode": "MAIN",
                "verifiedAt": "2026-10-12T00:00:00Z",
            },
        ),
        (
            "put",
            "/v1/supply/facts/demands",
            {
                "organizationId": organization_id,
                "sourceType": "manual",
                "sourceReference": "order",
                "lineKey": "1",
                "partNumber": "PART-A",
            },
        ),
        (
            "put",
            "/v1/supply/facts/receipts",
            {
                "organizationId": organization_id,
                "receiptReference": "receipt",
                "lineKey": "1",
                "partNumber": "PART-A",
                "status": "in_transit",
            },
        ),
    ]
    for method, path, payload in commands:
        response = getattr(api, method)(path, headers=headers, json=payload)
        assert response.status_code == 200
    assert len(supplies.created) == 4


def test_case_scope_accepts_only_bounded_flat_business_context() -> None:
    api, supplies = _client("buyer")
    response = api.post(
        "/v1/supply/cases",
        headers={"authorization": "Bearer fixed-token"},
        json={
            "organizationId": str(ALPHA_ID),
            "title": "PART-A shortage",
            "status": "open",
            "severity": "high",
            "scope": {
                "partNumbers": ["PART-A"],
                "locationCodes": ["MAIN"],
                "sourceReferences": ["work-order-9"],
                "tags": ["line-stop-risk"],
            },
        },
    )
    assert response.status_code == 200
    assert supplies.created[0]["scope"] == {
        "partNumbers": ["PART-A"],
        "locationCodes": ["MAIN"],
        "sourceReferences": ["work-order-9"],
        "tags": ["line-stop-risk"],
    }


def test_case_scope_rejects_unknown_or_nested_content() -> None:
    api, _ = _client("buyer")
    payload = {
        "organizationId": str(ALPHA_ID),
        "title": "Case",
        "status": "open",
        "severity": "low",
        "scope": {"partNumbers": [{"untrusted": "PART-A"}]},
    }
    nested = api.post(
        "/v1/supply/cases",
        headers={"authorization": "Bearer fixed-token"},
        json=payload,
    )
    assert nested.status_code == 422
    payload["scope"] = {"unexpected": ["value"]}
    unexpected = api.post(
        "/v1/supply/cases",
        headers={"authorization": "Bearer fixed-token"},
        json=payload,
    )
    assert unexpected.status_code == 422


def test_supply_command_denial_has_stable_error() -> None:
    class DeniedSupply(FakeSupplyRepository):
        async def upsert_inventory(self, connection, **values: object) -> UUID:
            del connection, values
            raise SupplyAccessError("没有供应链录入权限")

    api = TestClient(create_app(FixedAuth(), FakeTenantRepository(), DeniedSupply()))
    response = api.put(
        "/v1/supply/facts/inventory",
        headers={"authorization": "Bearer fixed-token"},
        json={
            "organizationId": str(ALPHA_ID),
            "partNumber": "PART-A",
            "locationCode": "MAIN",
            "verifiedAt": "2026-10-12T00:00:00Z",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "supply_forbidden"


def test_read_only_supply_role_cannot_mutate_any_supply_fact() -> None:
    """planner 保留供应读取能力，但没有 supply:propose，四类人工事实均不得写入。"""
    api, supplies = _client("planner")
    headers = {"authorization": "Bearer fixed-token"}
    organization_id = str(ALPHA_ID)
    commands = [
        (
            "post",
            "/v1/supply/cases",
            {
                "organizationId": organization_id,
                "title": "Case",
                "status": "open",
                "severity": "low",
            },
        ),
        (
            "put",
            "/v1/supply/facts/inventory",
            {
                "organizationId": organization_id,
                "partNumber": "PART-A",
                "locationCode": "MAIN",
                "verifiedAt": "2026-10-12T00:00:00Z",
            },
        ),
        (
            "put",
            "/v1/supply/facts/demands",
            {
                "organizationId": organization_id,
                "sourceType": "manual",
                "sourceReference": "plan",
                "lineKey": "1",
                "partNumber": "PART-A",
            },
        ),
        (
            "put",
            "/v1/supply/facts/receipts",
            {
                "organizationId": organization_id,
                "receiptReference": "receipt",
                "lineKey": "1",
                "partNumber": "PART-A",
                "status": "in_transit",
            },
        ),
    ]
    for method, path, payload in commands:
        response = getattr(api, method)(path, headers=headers, json=payload)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "capability_forbidden"
    assert supplies.created == []
