"""Simulated stock, in-transit and allocation rows for the first vertical slice.

Everything written here is flagged `is_simulated`, because a shortage computed from
invented stock must never be mistaken for a real one (BR-09). The generator is
deterministic — a fixed seed and hand-pinned values for the slice components — so the
same run twice produces the same database, which is what makes the numbers on screen
reproducible in a demo and stable in a test.

Five components are pinned by hand so that each of the four in-transit exclusion
reasons and the allocation-competition path are all exercised by real data rather than
by a unit test's fixture. The rest of the project's components get modest coverage so
the shortage table stays readable: only the pinned ones come out short.

Run with: make seed-supply
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text

from infrastructure.database import database_url

SEED = 20260922
TENANT = "default"
PROJECT = "Glasgow_revC3"

#: A stable id, so re-running replaces the same rows instead of piling up demands.
COMPETING_DEMAND = UUID("00000000-0000-4000-8000-0000000c0de1")

#: component_id -> (on_hand, quarantine, [(qty, confirmed, eta_offset_days, allocated)])
#: eta_offset_days = None means the supplier gave no date at all.
PINNED = {
    # 73/板 · 50 套 = 3650 需求。库存加按期在途仍不够 → 真实缺口 850。
    "part_16eb5ee0be0faa6d": (2000, 150, [(800, True, 30, None)]),
    # 17/板 = 850。在途 400 但 ETA 晚于需求日 → after_need_by_date 被排除。
    "part_7eafabef072d79ec": (300, 0, [(400, True, 120, None)]),
    # 4/板 = 200。唯一一批在途未确认 → unconfirmed 被排除，全额缺口。
    "part_19bbcd54c4c03cce": (0, 0, [(200, False, 20, None)]),
    # 14/板 = 700。库存够，但 500 已被另一个需求占用 → 资源竞争。
    "part_3b9355c297cd3387": (900, 0, [(300, True, 25, COMPETING_DEMAND)]),
    # 8/板 = 400。在途没有到货日期 → eta_unknown，不得按乐观假设折算。
    "part_9e327d14f3058d2a": (100, 0, [(500, True, None, None)]),
}
ALLOCATED_TO_COMPETITOR = {"part_3b9355c297cd3387": 500}


def project_components(connection) -> list[str]:
    rows = connection.execute(text("""
        SELECT DISTINCT c.component_id
        FROM bom_line b JOIN bom_line_candidate c USING(line_id)
        WHERE b.project_id = :project ORDER BY 1"""), {"project": PROJECT}).scalars().all()
    if not rows:
        raise ValueError(f"{PROJECT} has no BOM candidates; run make import-data first")
    return list(rows)


def clear_simulated(connection) -> None:
    """Idempotence. Only simulated rows go, so any real data stays untouched."""
    connection.execute(text("DELETE FROM inventory_allocation WHERE demand_id = :d"),
                       {"d": COMPETING_DEMAND})
    connection.execute(text("DELETE FROM in_transit WHERE is_simulated"))
    connection.execute(text("DELETE FROM inventory WHERE is_simulated"))
    connection.execute(text("DELETE FROM demand WHERE demand_id = :d"), {"d": COMPETING_DEMAND})


def seed(connection) -> dict[str, int]:
    components = project_components(connection)
    clear_simulated(connection)
    now = datetime.now(UTC)
    # UTC-anchored so a run at 23:00 local time does not shift every ETA by a day.
    today = now.date()
    rng = random.Random(SEED)
    counts = {"inventory": 0, "in_transit": 0, "inventory_allocation": 0}

    connection.execute(text("""
        INSERT INTO demand(demand_id, tenant_id, project_id, product_version, quantity,
                           need_by_date, created_by, is_simulated)
        VALUES (:id, :tenant, :project, 'sim-competing', 1, :need_by, 'seed', TRUE)"""),
        {"id": COMPETING_DEMAND, "tenant": TENANT, "project": PROJECT,
         "need_by": today + timedelta(days=45)})

    for component_id in components:
        if component_id in PINNED:
            on_hand, quarantine, transits = PINNED[component_id]
        else:
            # Comfortably covered: these lines exist to prove the计算 runs over the whole
            # BOM, not to produce more shortages to look at.
            on_hand, quarantine, transits = rng.randrange(4000, 12000), 0, []
        connection.execute(text("""
            INSERT INTO inventory(component_id, tenant_id, warehouse, on_hand_qty,
                                  quarantine_qty, snapshot_at, is_simulated)
            VALUES (:c, :t, 'MAIN', :oh, :q, :at, TRUE)"""),
            {"c": component_id, "t": TENANT, "oh": Decimal(on_hand),
             "q": Decimal(quarantine), "at": now})
        counts["inventory"] += 1

        for qty, confirmed, eta_offset, allocated in transits:
            connection.execute(text("""
                INSERT INTO in_transit(in_transit_id, tenant_id, component_id, qty,
                                       is_confirmed, eta, allocated_to, is_simulated)
                VALUES (:id, :t, :c, :qty, :ok, :eta, :alloc, TRUE)"""),
                {"id": uuid4(), "t": TENANT, "c": component_id, "qty": Decimal(qty),
                 "ok": confirmed,
                 "eta": None if eta_offset is None else today + timedelta(days=eta_offset),
                 "alloc": allocated})
            counts["in_transit"] += 1

    for component_id, qty in ALLOCATED_TO_COMPETITOR.items():
        connection.execute(text("""
            INSERT INTO inventory_allocation(allocation_id, tenant_id, component_id,
                                             demand_id, allocated_qty)
            VALUES (:id, :t, :c, :d, :qty)"""),
            {"id": uuid4(), "t": TENANT, "c": component_id,
             "d": COMPETING_DEMAND, "qty": Decimal(qty)})
        counts["inventory_allocation"] += 1
    return counts


def main() -> None:
    engine = create_engine(database_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            counts = seed(connection)
        print("Seeded simulated supply rows:", counts)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
