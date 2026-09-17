"""Real database fixtures; rolled back and checked for cleanup after each test."""
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from infrastructure.database import database_url

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def migrated():
    engine = create_engine(database_url(), hide_parameters=True)
    schema = "test_f01_" + uuid4().hex
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(text(f'CREATE SCHEMA "{schema}"'))
                conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                config = Config(str(ROOT / "alembic.ini"))
                config.attributes["connection"] = conn
                command.upgrade(config, "head")
                yield conn, config, schema
            finally:
                transaction.rollback()
        with engine.connect() as conn:
            assert not conn.scalar(text("SELECT 1 FROM pg_namespace WHERE nspname=:name"),
                                   {"name": schema})
    finally:
        engine.dispose()




def seed_procurement_case(conn):
    """Synthetic EV-01 data; no real BOM identity/quantity approvals are changed."""
    from datetime import date
    from decimal import Decimal

    from persistence.procurement import ProcurementRepository
    from procurement_core.demand import prepare_demand

    conn.execute(text("""INSERT INTO project(project_id,source_file,bom_lines,total_quantity)
                         VALUES ('synthetic','test fixture',1,1)"""))
    conn.execute(text("""INSERT INTO component(component_id,manufacturer,mpn,identity_status)
                         VALUES ('part','synthetic','test-part','verified')"""))
    conn.execute(text("""INSERT INTO bom_line(line_id,project_id,source_row_index,reference,
                         quantity,qty_basis_verified) VALUES ('line','synthetic',1,'R1',1,true)"""))
    conn.execute(text("""INSERT INTO bom_line_candidate VALUES
                         ('line',1,'part','synthetic','synthetic','test-part')"""))
    repo = ProcurementRepository(conn)
    demand_id, other_id, run_id = uuid4(), uuid4(), uuid4()
    for identifier in (demand_id, other_id):
        prepare_demand(repo, demand_id=identifier, project_id="synthetic", product_version="v1",
                       production_qty=Decimal(100), need_by_date=date(2026, 12, 1),
                       created_by="test", selected={"line": "part"}, is_simulated=True)
    conn.execute(text("INSERT INTO run(run_id,demand_id,trigger_kind,state) VALUES (:r,:d,'user','created')"),
                 {"r": run_id, "d": demand_id})
    conn.execute(text("""INSERT INTO inventory(component_id,warehouse,on_hand_qty,quarantine_qty,
                        snapshot_at,is_simulated) VALUES ('part','main',60,999,now(),true)"""))
    conn.execute(text("""INSERT INTO inventory_allocation(allocation_id,component_id,demand_id,
                        allocated_qty) VALUES (:id,'part',:other,10)"""),
                 {"id": uuid4(), "other": other_id})
    conn.execute(text("""INSERT INTO in_transit(in_transit_id,component_id,qty,is_confirmed,eta,
                        is_simulated) VALUES (:id,'part',20,true,'2026-12-01',true)"""),
                 {"id": uuid4()})
    return repo, demand_id, run_id


@pytest.fixture
def procurement_case(migrated):
    return seed_procurement_case(migrated[0])


@pytest.fixture
def committed_procurement_case():
    """Committed isolated schema, enabling independent concurrent connections."""
    from procurement_core.shortage import calculate_shortages

    engine = create_engine(database_url(), hide_parameters=True)
    schema = "test_concurrent_" + uuid4().hex
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
            repo, _, run_id = seed_procurement_case(conn)
            snapshots = calculate_shortages(repo, run_id)["snapshots"]
        yield engine, schema, run_id, [s["snapshot_id"] for s in snapshots]
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            assert not conn.scalar(text("SELECT 1 FROM pg_namespace WHERE nspname=:name"),
                                   {"name": schema})
        engine.dispose()
