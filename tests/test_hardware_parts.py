"""The hardware raw layer on real PostgreSQL: verbatim, complete, append-only."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from knowledge.fetch import RAW
from knowledge.kicad import raw_symbols
from persistence.import_hardware_raw import import_project

pytestmark = pytest.mark.integration
PROJECTS = ("hackrf-one", "jetson-agx-thor-baseboard", "bms-c1")


def test_every_file_round_trips_byte_for_byte(migrated):
    conn, _, _ = migrated
    for project_id in PROJECTS:
        import_project(conn, project_id)
    rows = conn.execute(text("SELECT path, sha256, content FROM hw_source_file")).all()
    assert len(rows) == 30
    assert all(hashlib.sha256(content.encode()).hexdigest() == sha for _, sha, content in rows)


def test_symbols_are_kept_whole_with_their_own_field_names(migrated):
    conn, _, _ = migrated
    import_project(conn, "hackrf-one")
    manifest = json.loads((RAW / "hackrf-one" / "MANIFEST.json").read_text())
    expected = raw_symbols(RAW / "hackrf-one" / manifest["root_schematic"])
    assert conn.scalar(text("SELECT count(*) FROM hw_raw_symbol")) == len(expected) == 732
    # Nothing renamed: HackRF says "Part Number", and so does the database.
    assert conn.scalar(text("SELECT count(*) FROM hw_raw_symbol WHERE properties ? 'MPN'")) == 0
    assert conn.scalar(text(
        "SELECT count(*) FROM hw_raw_symbol WHERE properties ? 'Part Number'")) > 300
    # Nothing filtered: power symbols stay.
    assert conn.scalar(text(
        "SELECT count(*) FROM hw_raw_symbol WHERE lib_id LIKE 'power:%'")) > 0


def test_reimporting_the_same_commit_changes_nothing(migrated):
    conn, _, _ = migrated
    first = import_project(conn, "bms-c1")
    again = import_project(conn, "bms-c1")
    assert again == {"project_id": "bms-c1", "status": "already imported",
                     "snapshot_id": first["snapshot_id"]}
    assert conn.scalar(text("SELECT count(*) FROM hw_source_snapshot")) == 1


def test_the_application_role_cannot_rewrite_the_raw_layer(migrated):
    conn, _, schema = migrated
    import_project(conn, "bms-c1")
    conn.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO supplyagent_app'))
    conn.execute(text("SAVEPOINT as_app"))
    conn.execute(text("SET LOCAL ROLE supplyagent_app"))
    with pytest.raises(DBAPIError):
        conn.execute(text("UPDATE hw_raw_symbol SET reference = 'X'"))
    conn.execute(text("ROLLBACK TO SAVEPOINT as_app"))
    conn.execute(text("RESET ROLE"))


def test_the_part_table_is_built_from_the_raw_layer_and_rebuilds_the_same(migrated):
    from persistence.build_hardware_parts import build_all

    conn, _, _ = migrated
    for project_id in PROJECTS:
        import_project(conn, project_id)
    first = build_all(conn)
    assert {r["project_id"]: r["parts"] for r in first} == {
        "hackrf-one": 62, "jetson-agx-thor-baseboard": 96, "bms-c1": 55}
    assert build_all(conn) == first
    assert conn.scalar(text("SELECT count(*) FROM hw_part")) == 213
    # Connector, spacer and battery are not certainly electronic, so they are left out;
    # a switch IC under a "...Switches" library stays in.
    kept = set(conn.scalars(text("SELECT mpn FROM hw_part")))
    assert not kept & {"USB4105-GF-A", "9774025151R", "MS621FE-FL11E"}
    assert "AP22615AWU-7" in kept
    row = conn.execute(text("""SELECT manufacturer, quantity, alternates FROM hw_part
                               WHERE mpn = 'CL05C220JB5NNNC'""")).one()
    assert row == ("Samsung", 12, "Murata GRM1555C1H220JA01D")
