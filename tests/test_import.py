"""F02 normalized data ingestion, exactness, idempotency and rollback."""
from decimal import Decimal

import pytest
from sqlalchemy import text

from persistence.import_normalized import TABLES, import_normalized, read_normalized

COUNTS = {"project": 5, "component": 142, "bom_line": 121,
              "bom_line_candidate": 148, "bom_line_distributor_sku": 160}


def test_source_counts_and_exact_quantities():
    rows = read_normalized()
    assert {table: len(values) for table, values in rows.items()} == COUNTS
    assert all(isinstance(row["quantity"], Decimal) for row in rows["bom_line"])
    candidate_lines = {row["line_id"] for row in rows["bom_line_candidate"]}
    assert len({row["line_id"] for row in rows["bom_line"]} - candidate_lines) == 3


@pytest.mark.integration
def test_import_repeat_preserves_rows_and_unverified_basis(migrated):
    conn, _, _ = migrated
    assert import_normalized(conn) == COUNTS
    before = {table: conn.execute(text(f"SELECT * FROM {table}")).all() for table in TABLES}
    assert import_normalized(conn) == COUNTS
    assert {table: conn.execute(text(f"SELECT * FROM {table}")).all()
            for table in TABLES} == before
    assert conn.scalar(text("SELECT count(*) FROM bom_line WHERE qty_basis_verified")) == 0
    assert conn.scalar(text("""SELECT count(*) FROM bom_line b WHERE NOT EXISTS
                            (SELECT 1 FROM bom_line_candidate c WHERE c.line_id=b.line_id)""")) == 3


@pytest.mark.integration
def test_import_conflict_rolls_back_earlier_inserts(migrated):
    conn, _, _ = migrated
    row = read_normalized()["component"][0]
    conn.execute(text("""INSERT INTO component
                      (component_id,manufacturer,mpn,identity_status)
                      VALUES (:component_id,:manufacturer,:mpn,'review_required')"""), row)
    with pytest.raises(ValueError, match="Static data conflict in component"):
        import_normalized(conn)
    assert conn.scalar(text("SELECT count(*) FROM project")) == 0
    assert conn.scalar(text("SELECT count(*) FROM component")) == 1
    assert conn.scalar(text("SELECT identity_status FROM component")) == "review_required"
