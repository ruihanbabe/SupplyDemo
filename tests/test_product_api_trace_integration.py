"""F40 Trace 加固的真实 PostgreSQL 负面与并发验收。"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, errors
from psycopg.rows import dict_row

from product_api.trace import PostgresTraceRepository


@pytest.fixture
def product_database_url() -> str:
    load_dotenv(".env")
    required = [
        "SUPPLYAGENT_PG_USER",
        "SUPPLYAGENT_PG_PASSWORD",
        "SUPPLYAGENT_PG_HOST",
        "SUPPLYAGENT_PG_PORT",
        "SUPPLYAGENT_PG_DB",
    ]
    if any(not os.getenv(name) for name in required):
        pytest.skip("本地 PostgreSQL 环境未配置")
    return "postgresql://{user}:{password}@{host}:{port}/{database}".format(
        user=os.environ["SUPPLYAGENT_PG_USER"],
        password=os.environ["SUPPLYAGENT_PG_PASSWORD"],
        host=os.environ["SUPPLYAGENT_PG_HOST"],
        port=os.environ["SUPPLYAGENT_PG_PORT"],
        database=os.environ["SUPPLYAGENT_PG_DB"],
    )


@pytest.mark.integration
def test_trace_rejects_cross_case_and_serializes_artifact_versions(
    product_database_url: str,
) -> None:
    asyncio.run(_scenario(product_database_url))


async def _scenario(database_url: str) -> None:
    organization_id, actor_id = uuid4(), uuid4()
    case_a, case_b, artifact_key = uuid4(), uuid4(), uuid4()
    await _insert_fixture(database_url, organization_id, actor_id, case_a, case_b)
    try:
        await _assert_cross_case_rejected(database_url, organization_id, actor_id, case_a, case_b, artifact_key)
        await _assert_artifact_readback_and_content_hash(database_url, organization_id, actor_id)
        barrier = asyncio.Barrier(2)
        versions = await asyncio.gather(
            _append_same_artifact(database_url, organization_id, actor_id, artifact_key, barrier),
            _append_same_artifact(database_url, organization_id, actor_id, artifact_key, barrier),
        )
        assert sorted(versions) == [1, 2]
    finally:
        await _cleanup_fixture(database_url, organization_id)


async def _assert_cross_case_rejected(
    database_url: str,
    organization_id: UUID,
    actor_id: UUID,
    case_a: UUID,
    case_b: UUID,
    artifact_key: UUID,
) -> None:
    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("set local role supplyagent_api")
            await cursor.execute(
                "select app.set_request_context(%s::uuid, %s::uuid)", (actor_id, organization_id)
            )
            await cursor.execute("select app.begin_trace_run('worker', %s::uuid) as id", (case_a,))
            row = await cursor.fetchone()
            assert row is not None
            run_id = row["id"]
        with pytest.raises(errors.InsufficientPrivilege):
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "select app.append_trace_artifact(%s, %s, %s, 'checkpoint', 'x', '{}'::jsonb)",
                        (run_id, case_b, artifact_key),
                    )
        with pytest.raises(errors.InsufficientPrivilege):
            async with connection.transaction():
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "select app.append_trace_event(%s, %s, 'case.mismatch', 'x', '{}'::jsonb, null)",
                        (run_id, case_b),
                    )
        await connection.rollback()


async def _append_same_artifact(
    database_url: str,
    organization_id: UUID,
    actor_id: UUID,
    artifact_key: UUID,
    barrier: asyncio.Barrier,
) -> int:
    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("set local role supplyagent_api")
            await cursor.execute(
                "select app.set_request_context(%s::uuid, %s::uuid)", (actor_id, organization_id)
            )
            await cursor.execute("select app.begin_trace_run('worker') as id")
            run = await cursor.fetchone()
            assert run is not None
            await barrier.wait()
            await cursor.execute(
                "select app.append_trace_artifact(%s, null, %s, 'checkpoint', 'x', '{}'::jsonb) as id",
                (run["id"], artifact_key),
            )
            artifact = await cursor.fetchone()
            assert artifact is not None
            await cursor.execute("select version from app.trace_artifact where id = %s", (artifact["id"],))
            row = await cursor.fetchone()
            assert row is not None
        await connection.commit()
        return int(row["version"])


async def _assert_artifact_readback_and_content_hash(
    database_url: str, organization_id: UUID, actor_id: UUID
) -> None:
    traces = PostgresTraceRepository()
    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("set local role supplyagent_api")
            await cursor.execute(
                "select app.set_request_context(%s::uuid, %s::uuid)", (actor_id, organization_id)
            )
        run_id = await traces.begin_run(connection, "worker", None)
        payload = {"part": "PART-A"}
        artifact_id = await traces.append_artifact(
            connection, run_id, None, uuid4(), "checkpoint", "first checkpoint", payload
        )
        artifact = await traces.get_artifact(connection, artifact_id)
        assert artifact is not None
        assert artifact.id == artifact_id
        assert artifact.run_id == run_id
        assert artifact.redacted_payload == payload
        listed = await traces.list_artifacts(connection, run_id)
        assert [item.id for item in listed] == [artifact_id]

        changed_type_id = await traces.append_artifact(
            connection, run_id, None, uuid4(), "checkpoint.v2", "first checkpoint", payload
        )
        changed_summary_id = await traces.append_artifact(
            connection, run_id, None, uuid4(), "checkpoint", "changed checkpoint", payload
        )
        changed_payload_id = await traces.append_artifact(
            connection, run_id, None, uuid4(), "checkpoint", "first checkpoint", {"part": "PART-B"}
        )
        changed = [
            await traces.get_artifact(connection, changed_type_id),
            await traces.get_artifact(connection, changed_summary_id),
            await traces.get_artifact(connection, changed_payload_id),
        ]
        assert all(item is not None for item in changed)
        assert all(item.content_hash != artifact.content_hash for item in changed if item is not None)
        await connection.commit()


async def _insert_fixture(
    database_url: str, organization_id: UUID, actor_id: UUID, case_a: UUID, case_b: UUID
) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "insert into app.organization (id, slug, display_name) values (%s, %s, 'F40 hardening')",
                (organization_id, f"f40-{organization_id.hex[:12]}"),
            )
            await cursor.execute(
                "insert into app.organization_member (organization_id, user_id, role) values (%s, %s, 'organization_admin')",
                (organization_id, actor_id),
            )
            await cursor.executemany(
                "insert into app.supply_case (id, organization_id, title, status, severity) values (%s, %s, %s, 'open', 'low')",
                [(case_a, organization_id, "Case A"), (case_b, organization_id, "Case B")],
            )
        await connection.commit()


async def _cleanup_fixture(database_url: str, organization_id: UUID) -> None:
    """测试专用：关闭 append-only trigger 只删除本次随机 fixture，随后立即恢复。"""
    async with await AsyncConnection.connect(database_url) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("alter table app.trace_event disable trigger trace_event_append_only")
            await cursor.execute("alter table app.trace_artifact disable trigger trace_artifact_append_only")
            try:
                await cursor.execute("delete from app.trace_event where organization_id = %s", (organization_id,))
                await cursor.execute("delete from app.trace_artifact where organization_id = %s", (organization_id,))
                await cursor.execute("delete from app.trace_run where organization_id = %s", (organization_id,))
                await cursor.execute("delete from app.supply_case where organization_id = %s", (organization_id,))
                await cursor.execute("delete from app.organization where id = %s", (organization_id,))
            finally:
                await cursor.execute("alter table app.trace_event enable trigger trace_event_append_only")
                await cursor.execute("alter table app.trace_artifact enable trigger trace_artifact_append_only")
        await connection.commit()
