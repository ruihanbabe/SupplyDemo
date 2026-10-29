"""Real RLS: old attempts cannot checkpoint or authorize another model request."""

import asyncio
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.gateway import GatewayError, ModelCapability, PostgresModelAccounting


@pytest.mark.integration
def test_checkpoint_and_model_capability_are_revoked_with_attempt():
    async def scenario():
        async with tool_database() as url:
            org, _, _, _, store, job = await seed_tool_job(url)
            scope = ModelCapability(store._identity.actor_id, org, job.trace_run_id, uuid4(),
                                    int(time.time()) + 60, job.id, job.generation, store._identity.owner)
            accounting = PostgresModelAccounting(store._tenants)

            async def allowed(c):
                await accounting._guard_job(c, scope)

            async def denied(c):
                await accounting._guard_job(c, replace(scope, generation=job.generation + 1))

            await store._tenants.in_organization(scope.actor_id, org, allowed)
            with pytest.raises(GatewayError):
                await store._tenants.in_organization(scope.actor_id, org, denied)
            assert await store.checkpoint(job, "claimed", {})
            assert not await store.checkpoint(replace(job, generation=job.generation + 1), "claimed", {})
            assert await store.complete(job)
            assert not await store.checkpoint(job, "before_terminal", {})
            with pytest.raises(GatewayError):
                await store._tenants.in_organization(scope.actor_id, org, allowed)
    asyncio.run(scenario())
