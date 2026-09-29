"""Fill hw_part straight from the pinned KiCad files: one row per purchasable part number
per maker per project.

The raw files under data/hardware/raw are the source; they are checked against MANIFEST
before anything is read, and the table is derived, so a rebuild replaces a project's rows
wholesale and rerunning gives the same table.

Run with: make build-parts
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, text

from infrastructure.database import database_url
from knowledge.fetch import RAW, load_registry, verify
from knowledge.kicad import read_schematic
from knowledge.parts import build_parts


def build_project(connection, project_id: str, raw: Path = RAW) -> dict:
    changed = verify(project_id, raw)
    if changed:
        raise ValueError(f"{project_id}: files differ from MANIFEST: {changed}")
    manifest = json.loads((raw / project_id / "MANIFEST.json").read_text(encoding="utf-8"))
    parts = build_parts(read_schematic(raw / project_id / manifest["root_schematic"]))
    connection.execute(text("DELETE FROM hw_part WHERE project_id = :project"),
                       {"project": project_id})
    if parts:
        connection.execute(text("""
            INSERT INTO hw_part(project_id, commit_sha, mpn, manufacturer, description,
                                "values", footprints, designators, quantity, alternates)
            VALUES (:project, :commit, :mpn, :manufacturer, :description,
                    :values, :footprints, :designators, :quantity, :alternates)"""),
            [{"project": project_id, "commit": manifest["commit"], "mpn": p.mpn,
              "manufacturer": p.manufacturer, "description": p.description,
              "values": p.values, "footprints": p.footprints, "designators": p.designators,
              "quantity": p.quantity, "alternates": p.alternates} for p in parts])
    return {"project_id": project_id, "parts": len(parts)}


def build_all(connection) -> list[dict]:
    return [build_project(connection, project["project_id"]) for project in load_registry()]


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
