"""Private SSE: ordered replay, cursor ownership and stream-time revocation."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_product_api_trace import (
    ACTOR_ID,
    ALPHA_ID,
    FIRST_EVENT_ID,
    OTHER_RUN_ID,
    RUN_ID,
    FakeTraces,
    FixedAuth,
)

from product_api.app import create_app
from product_api.tenant import TenantAccessError


class Tenants:
    async def in_organization_as_member(self, actor, organization, operation):
        if actor != ACTOR_ID or organization != ALPHA_ID:
            raise TenantAccessError("forbidden")
        return await operation(object(), SimpleNamespace(role="sales_manager"))


def client(monkeypatch, revoke=False):
    reads = []

    async def visible(connection, run):
        assert run == RUN_ID
        reads.append(run)
        if revoke and len(reads) > 1:
            raise TenantAccessError("revoked")
        return {"trace_run_id": RUN_ID, "status": "leased" if revoke else "completed"}

    monkeypatch.setattr("product_api.app.visible_request", visible)
    traces = FakeTraces()
    return TestClient(create_app(FixedAuth(), Tenants(), trace_repository=traces)), traces, reads


def get(c, cursor=None):
    return c.get(
        f"/v1/intelligence/runs/{RUN_ID}/events", params={"organization_id": str(ALPHA_ID)},
        headers={"authorization": "Bearer fixed-token", **({"last-event-id": str(cursor)} if cursor else {})},
    )


def test_private_stream_resumes_after_event_and_closes_at_terminal(monkeypatch):
    c, traces, reads = client(monkeypatch)
    response = get(c, FIRST_EVENT_ID)
    assert response.status_code == 200
    assert response.text.count("event: trace") == 1 and "event: complete" in response.text
    assert traces.cursors == [FIRST_EVENT_ID] and len(reads) == 1


@pytest.mark.parametrize("cursor", [OTHER_RUN_ID, "invalid"])
def test_private_stream_rejects_foreign_or_malformed_cursor_before_headers(monkeypatch, cursor):
    c, _, _ = client(monkeypatch)
    assert get(c, cursor).status_code == 403


def test_private_stream_rechecks_permission_between_batches(monkeypatch):
    c, _, reads = client(monkeypatch, revoke=True)
    response = get(c)
    assert response.text.count("event: trace") == 2
    assert "event: access_revoked" in response.text and len(reads) == 2


def test_private_stream_includes_events_created_after_initial_read(monkeypatch):
    reads = []

    async def visible(connection, run):
        reads.append(run)
        return {"trace_run_id": RUN_ID, "status": "leased" if len(reads) == 1 else "completed"}

    class GrowingTrace(FakeTraces):
        async def list_events(self, connection, run, cursor):
            assert run == RUN_ID
            if cursor is None:
                return self.events[:1]
            assert cursor == FIRST_EVENT_ID
            return self.events[1:]

    monkeypatch.setattr("product_api.app.visible_request", visible)
    c = TestClient(create_app(FixedAuth(), Tenants(), trace_repository=GrowingTrace()))
    response = get(c)
    assert response.text.count("event: trace") == 2
    assert response.text.index('"sequenceNo":1') < response.text.index('"sequenceNo":2')
    assert "event: complete" in response.text and len(reads) == 2
