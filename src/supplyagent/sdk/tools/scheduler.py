"""Ordered batches, contiguous compatibility groups, and shared SQL admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from supplyagent.paths import PROJECT_ROOT
from supplyagent.sdk.tools.runtime import PreparedCall, ToolAbort, ToolOutcome


@dataclass(frozen=True)
class SchedulerPolicy:
    strategy: str = "contiguous"
    poll_seconds: float = 0.05
    reservation_seconds: int = 120
    capacities: dict = field(default_factory=dict)
    renewal_seconds: float = 0.25

    def __post_init__(self):
        if (
            self.strategy not in {"serial", "contiguous"}
            or not math.isfinite(self.poll_seconds)
            or not 0 < self.poll_seconds <= 10
            or not 1 <= self.reservation_seconds <= 3600
            or not math.isfinite(self.renewal_seconds)
            or not 0 < self.renewal_seconds < min(self.reservation_seconds, 1)
        ):
            raise ValueError("invalid scheduler policy")
        if any(type(n) is not int or n < 1 for n in self.capacities.values()):
            raise ValueError("invalid resource capacity")
        object.__setattr__(self, "capacities", MappingProxyType(dict(self.capacities)))

    @property
    def snapshot(self):
        settings = {
            "strategy": self.strategy,
            "poll": self.poll_seconds,
            "reservation": self.reservation_seconds,
            "renewal": self.renewal_seconds,
            "capacities": dict(self.capacities),
        }
        return {
            "schemaVersion": 1,
            "strategy": self.strategy,
            "configurationHash": hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
            ).hexdigest(),
        }

    @classmethod
    def load(cls, path=None):
        path = (
            path or PROJECT_ROOT / "config/product/tool-runtime-policy.json"
        )
        p = json.loads(Path(path).read_text())
        if (
            set(p)
            != {
                "schemaVersion",
                "strategy",
                "admissionPollSeconds",
                "reservationSeconds",
                "capacities",
                "reservationHeartbeatSeconds",
            }
            or p["schemaVersion"] != 1
        ):
            raise ValueError("invalid tool policy schema")
        return cls(
            p["strategy"],
            p["admissionPollSeconds"],
            p["reservationSeconds"],
            p["capacities"],
            p["reservationHeartbeatSeconds"],
        )


def compatible(a, b):
    if not isinstance(a, PreparedCall) or not isinstance(b, PreparedCall):
        return False
    aa, ba = json.loads(a.arguments_json), json.loads(b.arguments_json)
    return a.spec.compatible(aa, b.spec, ba) and b.spec.compatible(ba, a.spec, aa)


class ToolScheduler:
    def __init__(self, policy=None):
        self.policy = policy or SchedulerPolicy.load()
        self._active = False

    async def run_batch(self, raw_calls, runtime, cancel=None, deadline=None):
        if self._active:
            raise ToolAbort("overlapping_batch")
        if not 1 <= len(raw_calls) <= 16 or [c[0].index for c in raw_calls] != list(
            range(len(raw_calls))
        ):
            raise ToolAbort("invalid_batch")
        identities = [c[0] for c in raw_calls]
        if len({(i.invocation_id, i.batch_id) for i in identities}) != 1 or len(
            {i.call_id for i in identities}
        ) != len(identities):
            raise ToolAbort("invalid_batch")
        self._active = True
        try:
            async with asyncio.timeout_at(deadline):
                # Preparation of the complete batch finishes before any domain call.
                prepared = []
                for identity, name, arguments in raw_calls:
                    self._guard(cancel, deadline)
                    prepared.append(
                        await runtime.prepare(
                            identity, name, arguments, policy=self.policy.snapshot
                        )
                    )
                results = []
                index = 0
                while index < len(prepared):
                    group = [prepared[index]]
                    index += 1
                    if self.policy.strategy == "contiguous":
                        while index < len(prepared) and all(
                            compatible(p, prepared[index]) for p in group
                        ):
                            group.append(prepared[index])
                            index += 1
                    tasks = [
                        asyncio.create_task(self._execute(p, runtime, cancel, deadline))
                        for p in group
                    ]
                    gathered = asyncio.gather(*tasks)
                    cancellation = (
                        asyncio.create_task(cancel.wait()) if cancel is not None else None
                    )
                    try:
                        if cancellation is not None:
                            done, _ = await asyncio.wait(
                                (gathered, cancellation), return_when=asyncio.FIRST_COMPLETED
                            )
                            if cancellation in done:
                                raise ToolAbort("cancelled")
                        results.extend(await gathered)
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await asyncio.gather(gathered, return_exceptions=True)
                        raise
                    finally:
                        if cancellation is not None:
                            cancellation.cancel()
                            await asyncio.gather(cancellation, return_exceptions=True)
                return results
        except TimeoutError:
            raise ToolAbort("deadline_exceeded") from None
        finally:
            self._active = False

    @staticmethod
    def _guard(cancel, deadline):
        if cancel is not None and cancel.is_set():
            raise ToolAbort("cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise ToolAbort("deadline_exceeded")

    async def _execute(self, call, runtime, cancel, deadline):
        self._guard(cancel, deadline)
        if isinstance(call, ToolOutcome):
            return call
        token, acquired, clean = uuid4(), False, False
        resources = []
        for resource in call.spec.resources:
            key = f"{resource.scope}:{resource.name}"
            capacity = self.policy.capacities.get(key)
            if capacity is None or resource.units > capacity or not runtime.store.durable:
                raise ToolAbort("resource_unconfigured")
            resources.append(
                {
                    "key": f"organization:{runtime.store.identity.organization_id}:{resource.name}"
                    if resource.scope == "organization"
                    else key,
                    "units": resource.units,
                    "capacity": capacity,
                }
            )
        try:
            if resources:
                while True:
                    self._guard(cancel, deadline)
                    await runtime.store.check(call.ticket)
                    if await runtime.store.acquire(
                        call.ticket, token, resources, self.policy.reservation_seconds
                    ):
                        acquired = True
                        break
                    if cancel is None:
                        await asyncio.sleep(self.policy.poll_seconds)
                    else:
                        try:
                            await asyncio.wait_for(cancel.wait(), self.policy.poll_seconds)
                        except TimeoutError:
                            pass
            self._guard(cancel, deadline)
            result = (
                await self._with_renewal(call, runtime, cancel, token)
                if acquired
                else await runtime.execute(call, cancel)
            )
            clean = True
            return result
        finally:
            if acquired:
                # Cancellation or uncertain remote completion keeps capacity quarantined.
                await asyncio.shield(runtime.store.release(call.ticket, token, clean))

    async def _with_renewal(self, call, runtime, cancel, token):
        async def renew():
            while True:
                await asyncio.sleep(self.policy.renewal_seconds)
                await runtime.store.renew(call.ticket, token, self.policy.reservation_seconds)

        execution = asyncio.create_task(runtime.execute(call, cancel))
        renewal = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait((execution, renewal), return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                renewal.result()
            return await execution
        finally:
            execution.cancel()
            renewal.cancel()
            await asyncio.gather(execution, renewal, return_exceptions=True)
