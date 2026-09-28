"""Fill hw_part from the raw layer: one row per purchasable part number per project.

Built from the database, not from files, so the table reflects exactly what was
imported. The table is derived, so a rebuild replaces a snapshot's rows wholesale.

Run with: make build-parts
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import create_engine, text

from infrastructure.database import database_url
from knowledge.kicad import RawSymbol, placements_from_raw
from knowledge.parts import build_parts


def _raw_symbols(connection, snapshot_id: UUID) -> list[RawSymbol]:
    rows = connection.execute(text("""
        SELECT sheet_path, file_path, symbol_uuid, lib_id, unit, reference, reference_source,
               attributes, properties
        FROM hw_raw_symbol WHERE snapshot_id = :id
        ORDER BY file_path, sheet_path, unit NULLS FIRST, symbol_uuid"""),
        {"id": snapshot_id}).mappings()
    return [RawSymbol(sheet_path=r["sheet_path"], file=r["file_path"],
                      symbol_uuid=r["symbol_uuid"], lib_id=r["lib_id"], unit=r["unit"],
                      reference=r["reference"], reference_source=r["reference_source"],
                      attributes=r["attributes"], properties=r["properties"])
            for r in rows]


def build_all(connection) -> list[dict]:
    snapshots = connection.execute(text("""
        SELECT DISTINCT ON (project_id) snapshot_id, project_id
        FROM hw_source_snapshot ORDER BY project_id, imported_at DESC""")).all()
    results = []
    for snapshot_id, project_id in snapshots:
        parts = build_parts(placements_from_raw(_raw_symbols(connection, snapshot_id)))
        connection.execute(text("DELETE FROM hw_part WHERE snapshot_id = :id"),
                           {"id": snapshot_id})
        if parts:
            connection.execute(text("""
                INSERT INTO hw_part(snapshot_id, mpn, manufacturer, description, quantity,
                                    alternates)
                VALUES (:id, :mpn, :manufacturer, :description, :quantity, :alternates)"""),
                [{"id": snapshot_id, "mpn": p.mpn, "manufacturer": p.manufacturer,
                  "description": p.description, "quantity": p.quantity,
                  "alternates": p.alternates} for p in parts])
        results.append({"project_id": project_id, "parts": len(parts)})
    return results


def main() -> None:
    engine = create_engine(database_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            for result in build_all(connection):
                print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
