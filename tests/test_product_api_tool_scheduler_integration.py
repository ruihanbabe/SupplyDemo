"""Independent processes contend for SQL capacity; crashed owners quarantine."""

import asyncio
import json
import sys
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from test_product_api_tool_runtime_integration import seed_tool_job, tool_database

from product_api.tool_registry import TOOL_REGISTRY
from product_api.tool_runtime import CallIdentity, ToolAbort

PROCESS = r"""
import json,sys,psycopg
from psycopg.types.json import Jsonb
p=json.loads(sys.stdin.readline())
for line in sys.stdin:
 command=json.loads(line)
 with psycopg.connect(p['url']) as c:
  c.execute('set local role supplyagent_api')
  c.execute('select app.set_request_context(%s,%s)',(p['worker'],p['org']))
  if command=='acquire':
   answer=c.execute('select app.tool_resource_acquire(%s,%s,%s,%s,60)',(p['execution'],p['owner'],p['token'],Jsonb([{'key':'deployment:fixture.pool','capacity':1,'units':1}]))).fetchone()[0]
  else:
   c.execute('select app.tool_resource_release(%s,%s,true)',(p['execution'],p['token']))
   answer=True
 print(json.dumps(answer),flush=True)
"""


@pytest.mark.integration
def test_two_processes_atomic_capacity_crash_and_quarantine():
    async def scenario():
        async with tool_database("0031_f51_tool_resources.sql") as url:
            children = []
            stores = []
            try:
                for _ in range(2):
                    org, _, worker, _, store, job = await seed_tool_job(url)
                    execution = store.execution_store(job)
                    binding = TOOL_REGISTRY.bind(("supply.impact",))
                    ticket = await execution.begin(
                        CallIdentity(uuid4(), "one"),
                        "supply.impact",
                        {"partNumber": "PART-A"},
                        binding.specs[0],
                        binding,
                        None,
                    )
                    token = uuid4()
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-c",
                        PROCESS,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                    )
                    proc.stdin.write(
                        (
                            json.dumps(
                                {
                                    "url": url,
                                    "worker": str(worker),
                                    "org": str(org),
                                    "execution": str(ticket["execution_id"]),
                                    "owner": store._identity.owner,
                                    "token": str(token),
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await proc.stdin.drain()
                    children.append(proc)
                    stores.append((execution, ticket, token))

                async def command(index, value):
                    p = children[index]
                    p.stdin.write((json.dumps(value) + "\n").encode())
                    await p.stdin.drain()
                    return json.loads(await asyncio.wait_for(p.stdout.readline(), 5))

                assert await command(0, "acquire") is True
                execution, ticket, token = stores[0]
                await execution.renew(ticket, token, 60)
                with pytest.raises(ToolAbort, match="lease_lost"):
                    await execution.renew(ticket, uuid4(), 60)
                second, waiting, _ = stores[1]
                assert not await second.acquire(
                    waiting,
                    uuid4(),
                    [
                        {"key": "deployment:fixture.available", "capacity": 1, "units": 1},
                        {"key": "deployment:fixture.pool", "capacity": 1, "units": 1},
                    ],
                    60,
                )
                async with await AsyncConnection.connect(url) as c:
                    assert (
                        await (
                            await c.execute(
                                "select count(*) from app.tool_resource_slot where execution_id=%s",
                                (waiting["execution_id"],),
                            )
                        ).fetchone()
                    )[0] == 0

                assert await command(1, "acquire") is False
                # A process death plus elapsed TTL does not prove remote completion.
                children[0].kill()
                await children[0].wait()
                async with await AsyncConnection.connect(url) as c:
                    await c.execute(
                        "update app.tool_resource_slot set expires_at=clock_timestamp()-interval '1 second'"
                    )
                execution, ticket, token = stores[0]

                async def reap(c):
                    return (
                        await (await c.execute("select app.tool_resource_reap() as n")).fetchone()
                    )["n"]

                assert await execution._transaction(reap) == 1
                assert await command(1, "acquire") is False
                with pytest.raises(ToolAbort, match="lease_lost"):
                    await execution.renew(ticket, token, 60)
                await execution.release(ticket, uuid4(), True)
                assert await command(1, "acquire") is False
                # Test coordinator explicitly knows this fixture has no remote operation.
                await execution.release(ticket, token, True)
                assert await command(1, "acquire") is True
                assert await command(1, "release") is True
            finally:
                for child in children:
                    if child.returncode is None:
                        child.stdin.close()
                        await child.wait()

    asyncio.run(scenario())
