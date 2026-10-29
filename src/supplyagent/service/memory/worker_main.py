"""Optional local-only F48 worker. Real data remains denied until deployment gates are met."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

from supplyagent.paths import PROJECT_ROOT
from supplyagent.service.memory.mem0_adapter import Mem0Adapter
from supplyagent.service.memory.runtime import MemoryWorker
from supplyagent.service.memory.store import MemoryStore
from supplyagent.service.persistence.tenant import PostgresTenantRepository
from supplyagent.service.runtime.runtime import WorkerIdentity


def load_memory_policy(path=None):
    path = (
        Path(path)
        if path
        else PROJECT_ROOT / "config/product/memory-policy.json"
    )
    policy = json.loads(path.read_text())
    if (
        set(policy)
        != {"schemaVersion", "enabled", "syntheticOnly", "backend", "retentionDays", "dreamEnabled"}
        or type(policy["schemaVersion"]) is not int
        or policy["schemaVersion"] != 1
        or type(policy["enabled"]) is not bool
        or policy["syntheticOnly"] is not True
        or policy["backend"] != "mem0"
        or policy["retentionDays"] != 7
        or type(policy["dreamEnabled"]) is not bool
    ):
        raise ValueError("memory production configuration is not enabled")
    return policy


def backend_factory():
    root = os.environ["SUPPLYAGENT_MEMORY_STORAGE_PATH"]
    endpoint = os.environ["SUPPLYAGENT_MEMORY_EMBEDDING_URL"]
    return lambda scope: Mem0Adapter(root, scope, embedding_url=endpoint, synthetic=True)


async def run_once():
    load_dotenv(".env")
    if not load_memory_policy()["enabled"]:
        return {"status": "disabled"}
    identity = WorkerIdentity(
        UUID(os.environ["SUPPLYAGENT_WORKER_ORGANIZATION_ID"]),
        UUID(os.environ["SUPPLYAGENT_WORKER_ACTOR_ID"]),
        os.environ["SUPPLYAGENT_WORKER_OWNER"],
    )
    store = MemoryStore(
        PostgresTenantRepository(os.environ["SUPPLYAGENT_PRODUCT_DATABASE_URL"]), identity
    )
    policy = load_memory_policy()
    if policy["dreamEnabled"]:
        from supplyagent.service.memory.dream import MemoryDream
        from supplyagent.service.memory.mem0_adapter import MemoryScope

        async def domains(c):
            return await (
                await c.execute(
                    "select distinct owner_id,object_scope from app.memory_source where state='ready'"
                )
            ).fetchall()

        for domain in await store.transaction(domains):
            await MemoryDream(store).schedule(
                MemoryScope(identity.organization_id, domain["owner_id"], domain["object_scope"]),
                enabled=True,
            )
    return await MemoryWorker(
        store,
        backend_factory(),
        summary_endpoint=os.environ["SUPPLYAGENT_MEMORY_SUMMARY_URL"],
    ).run_once()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(asyncio.run(run_once()), ensure_ascii=False))


if __name__ == "__main__":
    main()
