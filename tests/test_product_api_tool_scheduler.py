import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from product_api.harness import ToolDefinition
from product_api.tool_registry import TOOL_REGISTRY, ToolBinding
from product_api.tool_runtime import CallIdentity, ToolAbort, ToolRuntime
from product_api.tool_scheduler import SchedulerPolicy, ToolScheduler


def test_contiguous_barriers_reverse_completion_and_serial_ablation():
    async def scenario(strategy):
        order = []
        names = (
            "crm.opportunity_facts",
            "knowledge.question_evidence",
            "supply.bound_impact",
            "mission.claims",
        )
        specs = TOOL_REGISTRY.bind(names).specs
        binding = ToolBinding(tuple(replace(s, overlap=(i in (1, 2))) for i, s in enumerate(specs)))

        async def handler(i):
            order.append(("start", i))
            await asyncio.sleep(0.02 if i == 1 else 0)
            order.append(("end", i))
            return i

        runtime = ToolRuntime(
            tuple(ToolDefinition(n, lambda _, i=i: handler(i)) for i, n in enumerate(names)),
            binding,
        )
        invocation = uuid4()
        batch = [(CallIdentity(invocation, str(i), index=i), n, {}) for i, n in enumerate(names)]
        result = await ToolScheduler(SchedulerPolicy(strategy)).run_batch(batch, runtime)
        assert [r.payload for r in result] == list(range(4))
        assert order[:2] == [("start", 0), ("end", 0)] and order[-2:] == [("start", 3), ("end", 3)]
        assert (
            order.index(("end", 2)) < order.index(("end", 1))
            if strategy == "contiguous"
            else order.index(("end", 1)) < order.index(("start", 2))
        )

    for strategy in ("serial", "contiguous"):
        asyncio.run(scenario(strategy))


def test_shared_session_and_asymmetric_compatibility_are_barriers():
    from product_api.tool_scheduler import compatible

    async def scenario():
        base = TOOL_REGISTRY.bind(("supply.impact",)).specs[0]
        runtime = ToolRuntime((ToolDefinition(base.name, lambda _: None),), ToolBinding((base,)))
        a = await runtime.prepare(CallIdentity(uuid4(), "a"), base.name, {"partNumber": "A"})
        b = await runtime.prepare(CallIdentity(uuid4(), "b"), base.name, {"partNumber": "B"})
        assert compatible(a, b)
        assert not compatible(replace(a, spec=replace(base, compatibility=lambda *_: False)), b)
        locked = replace(base, exclusive_resources=("session:one",))
        assert not compatible(replace(a, spec=locked), replace(b, spec=locked))

    asyncio.run(scenario())


def test_hard_failure_cancels_sibling_and_does_not_cross_barrier():
    async def scenario():
        stopped = asyncio.Event()
        later = []
        names = ("knowledge.question_evidence", "supply.bound_impact", "mission.claims")
        binding = TOOL_REGISTRY.bind(names)
        binding = ToolBinding((*binding.specs[:2], replace(binding.specs[2], overlap=False)))

        async def fail(_):
            await asyncio.sleep(0)
            raise ToolAbort("lease_lost")

        async def pending(_):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        r = ToolRuntime(
            (
                ToolDefinition(names[0], fail),
                ToolDefinition(names[1], pending),
                ToolDefinition(names[2], lambda _: later.append(True)),
            ),
            binding,
        )
        inv = uuid4()
        with pytest.raises(ToolAbort, match="lease_lost"):
            await ToolScheduler().run_batch(
                [(CallIdentity(inv, str(i), index=i), n, {}) for i, n in enumerate(names)], r
            )
        assert stopped.is_set() and not later

    asyncio.run(scenario())


def test_deadline_cancels_execution_and_releases_active_batch():
    async def scenario():
        async def pending(_):
            await asyncio.Event().wait()

        r = ToolRuntime(
            (ToolDefinition("supply.impact", pending),), TOOL_REGISTRY.bind(("supply.impact",))
        )
        scheduler = ToolScheduler()
        with pytest.raises(ToolAbort, match="deadline_exceeded"):
            await scheduler.run_batch(
                [(CallIdentity(uuid4(), "a"), "supply.impact", {"partNumber": "A"})],
                r,
                deadline=asyncio.get_running_loop().time() + 0.02,
            )
        assert not scheduler._active

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["cancelled", "lease_lost"])
def test_admission_wait_rechecks_cancel_and_lease(reason):
    from types import SimpleNamespace

    from product_api.tool_registry import ToolResource
    from product_api.tool_runtime import MemoryExecutionStore

    class Store(MemoryExecutionStore):
        durable = True
        identity = SimpleNamespace(organization_id=uuid4())
        checks = 0

        async def check(self, ticket):
            self.checks += 1
            if reason == "lease_lost" and self.checks >= 2:
                raise ToolAbort("lease_lost")

        async def acquire(self, *_):
            return False

    async def scenario():
        store = Store()
        cancel = asyncio.Event()
        spec = replace(
            TOOL_REGISTRY.bind(("supply.impact",)).specs[0],
            resources=(ToolResource("fixture.pool"),),
        )
        runtime = ToolRuntime(
            (
                ToolDefinition(
                    spec.name, lambda _: pytest.fail("must not execute"), execution_store=store
                ),
            ),
            ToolBinding((spec,)),
        )
        scheduler = ToolScheduler(
            SchedulerPolicy(poll_seconds=0.005, capacities={"organization:fixture.pool": 1})
        )

        async def stop():
            await asyncio.sleep(0.015)
            if reason == "cancelled":
                cancel.set()

        task = asyncio.create_task(stop())
        try:
            with pytest.raises(ToolAbort, match=reason):
                await scheduler.run_batch(
                    [(CallIdentity(uuid4(), "a"), spec.name, {"partNumber": "A"})],
                    runtime,
                    cancel,
                    asyncio.get_running_loop().time() + 1,
                )
        finally:
            await task

    asyncio.run(scenario())


def test_reservation_renewal_failure_cancels_operation_and_quarantines():
    from types import SimpleNamespace

    from product_api.tool_registry import ToolResource
    from product_api.tool_runtime import MemoryExecutionStore

    class Store(MemoryExecutionStore):
        durable = True
        identity = SimpleNamespace(organization_id=uuid4())
        released = None

        async def acquire(self, *_):
            return True

        async def renew(self, *_):
            raise ToolAbort("lease_lost")

        async def release(self, ticket, token, clean):
            self.released = clean

    async def scenario():
        store = Store()
        stopped = asyncio.Event()

        async def pending(_):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        spec = replace(
            TOOL_REGISTRY.bind(("supply.impact",)).specs[0],
            resources=(ToolResource("fixture.pool"),),
        )
        runtime = ToolRuntime(
            (ToolDefinition(spec.name, pending, execution_store=store),), ToolBinding((spec,))
        )
        with pytest.raises(ToolAbort, match="lease_lost"):
            await ToolScheduler(
                SchedulerPolicy(capacities={"organization:fixture.pool": 1}, renewal_seconds=0.01)
            ).run_batch([(CallIdentity(uuid4(), "a"), spec.name, {"partNumber": "A"})], runtime)
        assert stopped.is_set() and store.released is False

    asyncio.run(scenario())
