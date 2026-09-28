"""Simulated stock, in-transit and allocation rows for the first vertical slice.

Everything written here is flagged `is_simulated`, because a shortage computed from
invented stock must never be mistaken for a real one (BR-09). The generator is
deterministic — a fixed seed and hand-pinned values for the slice components — so the
same run twice produces the same database, which is what makes the numbers on screen
reproducible in a demo and stable in a test.

Five single-candidate components are pinned by hand so that each of the four in-transit
exclusion reasons and the allocation-competition path are exercised by real data rather
than by a unit test's fixture. Two candidates of the multi-candidate line R2 are pinned
as well: once a person picks one, the other's stock is what `internal` reports. The rest
get comfortable coverage so only the pinned ones come out short.

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
PROJECT = "PCB-Stimulator"

#: A stable id, so re-running replaces the same rows instead of piling up demands.
COMPETING_DEMAND = UUID("00000000-0000-4000-8000-0000000c0de1")

#: component_id -> (on_hand, quarantine, [(qty, confirmed, eta_offset_days, allocated)])
#: eta_offset_days = None means the supplier gave no date at all.
PINNED = {
    # J6 B2B-XH-A · 8/板 · 50 套 = 400。库存加按期在途仍不够 → 真实缺口 150。
    "part_fafb3ac786767f64": (150, 20, [(100, True, 30, None)]),
    # J10 SSW-116-02-G-S · 5/板 = 250。在途 300 但 ETA 晚于需求日 → after_need_by_date。
    "part_b0bcdcfc0abc0a15": (50, 0, [(300, True, 120, None)]),
    # L3 SSH-LX5091 · 4/板 = 200。唯一一批在途未确认 → unconfirmed，全额缺口。
    "part_6702f27d2b4ba574": (0, 0, [(200, False, 20, None)]),
    # J3 XHP-2 · 8/板 = 400。库存 600，但 300 已被另一个需求占用 → 资源竞争，缺口 100。
    "part_813126b820f877cb": (600, 0, [(300, True, 25, COMPETING_DEMAND)]),
    # L1 5219210F · 4/板 = 200。在途没有到货日期 → eta_unknown，不得按乐观假设折算。
    "part_007bc9f269f1daf4": (50, 0, [(300, True, None, None)]),
    # R2 多候选行 · 1/板 = 50。选 RS Pro 707-7647 则无货；Viking Tech 候选在库 500，
    # 这是 internal 在人工选定后要报告的「替代候选可用量」。
    "part_d43c367a402aa887": (0, 0, []),
    "part_06ec62d95082cd3d": (500, 0, []),
}
ALLOCATED_TO_COMPETITOR = {"part_813126b820f877cb": 300}


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
            # Comfortably covered: these lines exist to prove the calculation runs over the whole
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
