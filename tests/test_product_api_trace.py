"""F40 Event-ID SSE 回放的离线 API 契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from product_api.app import create_app
from product_api.auth import AuthenticatedIdentity, AuthenticationError
from product_api.tenant import OrganizationMembership, TenantAccessError
from product_api.trace import TraceAccessError, TraceEvent, TraceRun

ACTOR_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ALPHA_ID = UUID("11111111-1111-1111-1111-111111111111")
BRAVO_ID = UUID("22222222-2222-2222-2222-222222222222")
RUN_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_RUN_ID = UUID("44444444-4444-4444-4444-444444444444")
FIRST_EVENT_ID = UUID("55555555-5555-5555-5555-555555555555")
SECOND_EVENT_ID = UUID("66666666-6666-6666-6666-666666666666")


class FixedAuth:
    async def verify_access_token(self, access_token: str) -> AuthenticatedIdentity:
        if access_token != "fixed-token":
            raise AuthenticationError("访问令牌无效")
        return AuthenticatedIdentity(ACTOR_ID, "viewer@example.test")


class FakeTenants:
    async def list_organizations(self, actor_id: UUID) -> list[OrganizationMembership]:
        del actor_id
        return []

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
        del actor_id, organization_id, operation
        raise AssertionError("not used")


class FakeTraces:
    def __init__(self) -> None:
        self.cursors: list[UUID | None] = []
        self.events = [
            _event(FIRST_EVENT_ID, 1, "run.started"),
            _event(SECOND_EVENT_ID, 2, "tool.completed"),
        ]

    async def get_run(self, connection, run_id: UUID) -> TraceRun | None:
        del connection
        return _run() if run_id == RUN_ID else None

    async def list_events(self, connection, run_id: UUID, after_event_id: UUID | None):
        del connection
        assert run_id == RUN_ID
        self.cursors.append(after_event_id)
        if after_event_id not in (None, FIRST_EVENT_ID):
            raise TraceAccessError("事件游标不属于当前组织或 Run")
        return self.events if after_event_id is None else self.events[1:]


def _client() -> tuple[TestClient, FakeTraces]:
    traces = FakeTraces()
    return TestClient(create_app(FixedAuth(), FakeTenants(), trace_repository=traces)), traces


def _run() -> TraceRun:
    return TraceRun(RUN_ID, None, "investigation", ACTOR_ID, datetime(2026, 10, 21, tzinfo=UTC), 3, "a" * 64)


def _event(event_id: UUID, sequence_no: int, event_type: str) -> TraceEvent:
    return TraceEvent(
        event_id, RUN_ID, sequence_no, event_type, "redacted", {"source": "fixture"}, None,
        None, "b" * 64, datetime(2026, 10, 21, tzinfo=UTC),
    )


def test_trace_sse_replays_after_last_event_id_header() -> None:
    api, traces = _client()
    response = api.get(
        f"/v1/traces/runs/{RUN_ID}/events",
        params={"organization_id": str(ALPHA_ID), "after_event_id": str(SECOND_EVENT_ID)},
        headers={"authorization": "Bearer fixed-token", "last-event-id": str(FIRST_EVENT_ID)},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert traces.cursors == [FIRST_EVENT_ID]
    assert response.text.startswith(f"id: {SECOND_EVENT_ID}\nevent: trace\ndata: ")
    assert '"sequenceNo":2' in response.text


def test_trace_sse_rejects_foreign_organization_before_trace_read() -> None:
    api, traces = _client()
    response = api.get(
        f"/v1/traces/runs/{RUN_ID}/events",
        params={"organization_id": str(BRAVO_ID)},
        headers={"authorization": "Bearer fixed-token"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "organization_forbidden"
    assert traces.cursors == []


def test_trace_sse_rejects_foreign_run_and_cursor() -> None:
    api, _ = _client()
    foreign_run = api.get(
        f"/v1/traces/runs/{OTHER_RUN_ID}/events",
        params={"organization_id": str(ALPHA_ID)},
        headers={"authorization": "Bearer fixed-token"},
    )
    assert foreign_run.status_code == 403
    assert foreign_run.json()["error"]["code"] == "trace_forbidden"

    foreign_cursor = api.get(
        f"/v1/traces/runs/{RUN_ID}/events",
        params={"organization_id": str(ALPHA_ID), "after_event_id": str(SECOND_EVENT_ID)},
        headers={"authorization": "Bearer fixed-token"},
    )
    assert foreign_cursor.status_code == 403
    assert foreign_cursor.json()["error"]["code"] == "trace_forbidden"
