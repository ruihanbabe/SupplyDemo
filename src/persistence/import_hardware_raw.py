"""Load the pinned KiCad sources into the hardware raw layer, verbatim.

Nothing is interpreted here: no field unification, no DNP reading, no grouping. The
file text goes in whole, and sheets and symbols are split out only along KiCad's own
structure so they can be queried. A snapshot already imported for the same commit is
skipped, never rewritten — the layer is append-only (data-model.md §13).

Run with: make import-hardware
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from infrastructure.database import database_url
from knowledge.fetch import RAW, load_registry, verify
from knowledge.kicad import raw_symbols, walk


def import_project(connection, project_id: str, raw: Path = RAW) -> dict[str, int | str]:
    source = raw / project_id
    corrupted = verify(project_id, raw)
    if corrupted:
        # The raw layer is the evidence of record; loading altered bytes would launder them.
        raise ValueError(f"{project_id}: files differ from MANIFEST: {corrupted}")
    manifest_bytes = (source / "MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_bytes)

    existing = connection.execute(text("""
        SELECT snapshot_id FROM hw_source_snapshot
        WHERE project_id = :project AND commit_sha = :commit"""),
        {"project": project_id, "commit": manifest["commit"]}).scalar()
    if existing is not None:
        return {"project_id": project_id, "status": "already imported",
                "snapshot_id": str(existing)}

    snapshot_id = uuid4()
    connection.execute(text("""
        INSERT INTO hw_source_snapshot(snapshot_id, project_id, repo, commit_sha, source_path,
                                       root_schematic, license, manifest_sha256, fetched_at)
        VALUES (:id, :project, :repo, :commit, :path, :root, :license, :sha, :fetched)"""),
        {"id": snapshot_id, "project": project_id, "repo": manifest["repo"],
         "commit": manifest["commit"], "path": manifest["path"],
         "root": manifest["root_schematic"], "license": manifest["license"],
         "sha": hashlib.sha256(manifest_bytes).hexdigest(),
         "fetched": datetime.fromisoformat(manifest["fetched_at"])})

    for path, meta in manifest["files"].items():
        connection.execute(text("""
            INSERT INTO hw_source_file(snapshot_id, path, sha256, git_blob, byte_size, content)
            VALUES (:id, :path, :sha, :blob, :size, :content)"""),
            {"id": snapshot_id, "path": path, "sha": meta["sha256"], "blob": meta["git_blob"],
             "size": meta["bytes"], "content": (source / path).read_text(encoding="utf-8")})

    root_file = source / manifest["root_schematic"]
    visits, _ = walk(root_file)
    connection.execute(text("""
        INSERT INTO hw_raw_sheet(snapshot_id, sheet_path, parent_path, file_path, sheet_uuid,
                                 properties)
        VALUES (:id, :path, :parent, :file, :uuid, CAST(:props AS JSONB))"""),
        [{"id": snapshot_id, "path": visit.sheet_path, "parent": visit.parent_path,
          "file": visit.file.relative_to(root_file.parent).as_posix(),
          "uuid": visit.sheet_uuid, "props": json.dumps(visit.properties, ensure_ascii=False)}
         for visit in visits])

    symbols = raw_symbols(root_file)
    connection.execute(text("""
        INSERT INTO hw_raw_symbol(snapshot_id, sheet_path, symbol_uuid, file_path, reference,
                                  reference_source, lib_id, unit, attributes, properties)
        VALUES (:id, :path, :uuid, :file, :ref, :source, :lib, :unit,
                CAST(:attrs AS JSONB), CAST(:props AS JSONB))"""),
        [{"id": snapshot_id, "path": s.sheet_path, "uuid": s.symbol_uuid, "file": s.file,
          "ref": s.reference, "source": s.reference_source, "lib": s.lib_id, "unit": s.unit,
          "attrs": json.dumps(s.attributes, ensure_ascii=False),
          "props": json.dumps(s.properties, ensure_ascii=False)}
         for s in symbols])

    return {"project_id": project_id, "status": "imported", "snapshot_id": str(snapshot_id),
            "files": len(manifest["files"]), "sheets": len(visits), "symbols": len(symbols)}


def main() -> None:
    engine = create_engine(database_url(), hide_parameters=True)
    try:
        with engine.begin() as connection:
            for project in load_registry():
                print(json.dumps(import_project(connection, project["project_id"]),
                                 ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
