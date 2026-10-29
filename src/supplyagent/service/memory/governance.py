"""Optional M2 governance; object identity is the existing trusted exact scope."""

from supplyagent.service.memory.mem0_adapter import admit


class MemoryGovernance:
    def __init__(self, store):
        self.store = store

    async def qualify(
        self, version, *, event_at=None, valid_from=None, valid_to=None, conditions=""
    ):
        admit(conditions or "unconditional")

        async def operation(c):
            await c.execute(
                "select app.memory_qualify(%s,%s,%s,%s,%s)",
                (version, event_at, valid_from, valid_to, conditions),
            )

        await self.store.transaction(operation)

    async def relate(self, version, other, kind):
        async def operation(c):
            await c.execute("select app.memory_relate(%s,%s,%s)", (version, other, kind))

        await self.store.transaction(operation)

    async def describe(self, version):
        async def operation(c):
            # Canonical metadata is returned only after the same source gate as recall.
            row = await (
                await c.execute(
                    "select event_at,valid_from,valid_to,conditions,coalesce(to_jsonb(v)->>'temperature','hot') as temperature from app.memory_version v where id=%s and app.memory_version_readable(id)",
                    (version,),
                )
            ).fetchone()
            if not row:
                return None
            relations = await (
                await c.execute(
                    "select from_id,to_id,kind from app.memory_relation where (from_id=%s or to_id=%s) and app.memory_version_readable(from_id) and app.memory_version_readable(to_id)",
                    (version, version),
                )
            ).fetchall()
            return {
                **{k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in row.items()},
                "relations": [{k: str(v) for k, v in r.items()} for r in relations],
                "authority": "historical_reference_not_current_business_fact",
            }

        return await self.store.transaction(operation)

    async def export_legacy(self):
        async def operation(c):
            return await (
                await c.execute(
                    "select id,status,source_kind,source_version_id,proposed_by,confirmed_by,created_at,case when app.memory_legacy_readable(id) then content end as content,case when app.memory_legacy_readable(id) then source_ref end as source_ref from app.long_term_memory where proposed_by=%s order by id",
                    (self.store.actor.actor_id,),
                )
            ).fetchall()

        return await self.store.transaction(operation)

    async def import_legacy(self, legacy_id, scope):
        async def operation(c):
            return (
                await (
                    await c.execute(
                        "select app.memory_import_legacy(%s,%s) as id", (legacy_id, scope)
                    )
                ).fetchone()
            )["id"]

        return await self.store.transaction(operation)

    async def reconcile_legacy(self, version):
        async def operation(c):
            await c.execute("select app.memory_reconcile_legacy(%s)", (version,))

        await self.store.transaction(operation)

    async def route(self, route=None):
        async def operation(c):
            if route is not None:
                await c.execute("select app.memory_set_read_route(%s)", (route,))
            return (
                await (await c.execute("select app.memory_read_current() as items")).fetchone()
            )["items"]

        return await self.store.transaction(operation)
