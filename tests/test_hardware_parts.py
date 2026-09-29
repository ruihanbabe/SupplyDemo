"""hw_part on real PostgreSQL, built straight from the pinned KiCad files."""
from __future__ import annotations

import os
import shutil
import stat

import pytest
from sqlalchemy import text

from knowledge.fetch import RAW
from persistence.build_hardware_parts import build_all, build_project

pytestmark = pytest.mark.integration


def test_the_part_table_is_built_from_the_files_and_rebuilds_the_same(migrated):
    conn, _, _ = migrated
    first = build_all(conn)
    assert first == [{"project_id": "hackrf-one", "parts": 62},
                     {"project_id": "bms-c1", "parts": 55}]
    assert build_all(conn) == first
    assert conn.scalar(text("SELECT count(*) FROM hw_part")) == 62 + 55
    row = conn.execute(text("""SELECT manufacturer, quantity, "values", footprints, alternates
                               FROM hw_part WHERE mpn = 'CL05C220JB5NNNC'""")).one()
    assert row[:2] == ("Samsung", 12) and row[4] == "Murata GRM1555C1H220JA01D"
    # Two values written on one part number (470 vs 475 on HackRF) are both kept.
    values = conn.scalar(text("""SELECT "values" FROM hw_part WHERE mpn = 'RMCF0402JT470R'"""))
    assert len(values) > 1


def test_files_that_differ_from_the_manifest_are_refused(migrated, tmp_path):
    conn, _, _ = migrated
    shutil.copytree(RAW / "hackrf-one", tmp_path / "hackrf-one")
    licence = tmp_path / "hackrf-one" / "hardware" / "hackrf-one" / "LICENSE"
    os.chmod(licence, stat.S_IWUSR | stat.S_IRUSR)
    licence.write_text("changed")
    with pytest.raises(ValueError, match="differ from MANIFEST"):
        build_project(conn, "hackrf-one", tmp_path)
