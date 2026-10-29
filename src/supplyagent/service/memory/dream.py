"""Lossless synthetic Dream. Jobs reuse MemoryWorker leases, metering and erasure."""

from supplyagent.service.memory.mem0_adapter import MemoryBackendError, admit


class MemoryDream:
    def __init__(self, store):
        self.store = store

    async def schedule(self, scope, *, enabled=False):
        if type(enabled) is not bool:
            raise MemoryBackendError("memory_denied")
        if not enabled:
            return None
        if scope.organization_id != self.store.actor.organization_id:
            raise MemoryBackendError("memory_denied")

        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.memory_schedule_dream(%s,%s,%s) as id",
                        (scope.user_id, scope.object_scope, enabled),
                    )
                ).fetchone()
            )["id"]

        return await self.store.transaction(operation)

    async def propose_lesson(self, version, text):
        admit(text)

        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.memory_lesson_candidate(%s,%s) as id", (version, text)
                    )
                ).fetchone()
            )["id"]

        return await self.store.transaction(operation)

    async def cool(self, version, *, cold=True):
        async def operation(c):
            await c.execute("select app.memory_cool(%s,%s)", (version, cold))

        await self.store.transaction(operation)

    async def independent_sources(self, version):
        async def operation(c):
            return [
                r["id"]
                for r in await (
                    await c.execute("select id from app.memory_roots(%s)", (version,))
                ).fetchall()
            ]

        return await self.store.transaction(operation)
