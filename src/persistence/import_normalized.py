"""Import the normalized static dataset atomically, without overwriting existing facts."""
import csv
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import MetaData, Table, create_engine, select
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import ROOT, database_url

NORMALIZED = ROOT / "data/supplychain/normalized"
TABLES = ("project", "component", "bom_line", "bom_line_candidate", "bom_line_distributor_sku")


def read_normalized(directory: Path = NORMALIZED) -> dict[str, list[dict]]:
    """Consume only normalized columns; never infer identity or quantity approval."""
    projects = yaml.safe_load((directory / "projects.yaml").read_text())["projects"]
    rows = {"project": [{"project_id": p["project_id"], "github_url": p.get("github"),
                             "kitspace_url": p.get("kitspace"), "source_file": p["source_file"],
                             "bom_lines": p["bom_lines"], "total_quantity": p["total_quantity"]}
                        for p in projects]}
    for name in TABLES[1:]:
        with (directory / f"{name}.csv").open(newline="") as stream:
            rows[name] = list(csv.DictReader(stream))
    for row in rows["component"]:
        row["used_by_count"] = int(row["used_by_count"])
    for row in rows["bom_line"]:
        row["source_row_index"] = int(row["source_row_index"])
        row["quantity"] = Decimal(row["quantity"])
        if not row["quantity"].is_finite() or row["quantity"] <= 0:
            raise ValueError("Invalid normalized BOM quantity")
    for row in rows["bom_line_candidate"]:
        row["candidate_seq"] = int(row["candidate_seq"])
    return rows


def import_normalized(connection, directory: Path = NORMALIZED) -> dict[str, int]:
    """Use the caller's transaction; a savepoint rolls back this import on conflict."""
    dataset = read_normalized(directory)
    counts = {}
    with connection.begin_nested():
        for name in TABLES:
            table = Table(name, MetaData(), autoload_with=connection)
            rows = dataset[name]
            columns = list(rows[0]) if rows else []
            keys = [column.name for column in table.primary_key]
            expected = {tuple(row[k] for k in keys): row for row in rows}
            if len(expected) != len(rows):
                raise ValueError(f"Duplicate source key in {name}")
            if rows:
                connection.execute(insert(table).on_conflict_do_nothing(), rows)
                actual = [dict(row) for row in connection.execute(
                    select(*(table.c[c] for c in columns))).mappings()]
                if {tuple(row[k] for k in keys): row for row in actual} != expected:
                    raise ValueError(f"Static data conflict in {name}; import rolled back")
            counts[name] = len(rows)
    return counts


def main():
    engine = create_engine(database_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            counts = import_normalized(connection)
        print("Imported static rows:", counts)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
