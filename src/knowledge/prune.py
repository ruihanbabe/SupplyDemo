"""Remove from the raw schematics every part that is not certainly an electronic component.

The raw layer keeps only electronic components. This step runs right after a fetch, cutting
whole `(symbol ...)` blocks out of the files byte-for-byte, so everything else stays exactly
as upstream wrote it. Which parts go is decided by `knowledge.parts.is_electronic`, the same
rule the part table uses. MANIFEST.json is rewritten with the new SHA-256 of each changed
file, keeps the upstream hash and git blob beside it, and records which part numbers were
removed, so the files can still be traced to the pinned commit.

Run with: make prune-hardware (make fetch-hardware already runs it)
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from knowledge.fetch import RAW, load_registry
from knowledge.kicad import parse_sexpr, placements_from_raw, properties, raw_symbols
from knowledge.parts import MPN_FIELDS, is_electronic, is_fitted, pick

Span = tuple[int, int]


def child_spans(text: str, start: int, end: int) -> list[Span]:
    """Byte spans of the lists directly inside the list at text[start:end]."""
    spans, depth, i, open_at = [], 0, start, 0
    while i < end:
        ch = text[i]
        if ch == '"':
            # Skip a quoted string, honouring backslash escapes.
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == "(":
            depth += 1
            if depth == 2:
                open_at = i
        elif ch == ")":
            if depth == 2:
                spans.append((open_at, i + 1))
            depth -= 1
        i += 1
    return spans


def _head(text: str, span: Span) -> str:
    # First token after "(", e.g. "symbol" for "(symbol (lib_id ...".
    return text[span[0] + 1:span[1]].split(None, 1)[0].rstrip(")")


def _cut(text: str, spans: list[Span]) -> str:
    """Drop each span with the newline and indentation that lead into it."""
    for start, end in sorted(spans, reverse=True):
        line_start = text.rfind("\n", 0, start)
        if line_start >= 0 and text[line_start + 1:start].strip() == "":
            start = line_start
        text = text[:start] + text[end:]
    return text


def removed_mpns(root_file: Path) -> set[str]:
    """Part numbers of fitted BOM parts with a placement that is not certainly electronic."""
    return {pick(p.properties, MPN_FIELDS)
            for p in placements_from_raw(raw_symbols(root_file))
            if p.in_bom and pick(p.properties, MPN_FIELDS) and is_fitted(p)
            and not is_electronic(p)}


def prune_text(text: str, drop: set[str], project_uuids: set[str]) -> tuple[str, list[str]]:
    """The file without symbols whose part number is in `drop`, and the removed symbol uuids.

    `project_uuids` are the uuids of every symbol being removed anywhere in the project:
    the KiCad 6 instance table sits in the root file, but the symbols it lists sit in sub-sheets.
    """
    # Scanning the whole file, the root "(kicad_sch" is depth 1, so these are its children.
    top = child_spans(text, 0, len(text))
    cuts, uuids, dropped_libs, kept_libs = [], [], set(), set()
    for span in top:
        if _head(text, span) != "symbol":
            continue
        node = parse_sexpr(text[span[0]:span[1]])
        lib_id = next(c[1] for c in node[1:] if isinstance(c, list) and c[0] == "lib_id")
        if pick(properties(node), MPN_FIELDS) in drop:
            cuts.append(span)
            uuids.append(next(c[1] for c in node[1:] if isinstance(c, list) and c[0] == "uuid"))
            dropped_libs.add(lib_id)
        else:
            kept_libs.add(lib_id)
    for span in top:
        head = _head(text, span)
        if head == "lib_symbols":
            # Library definitions only the removed symbols used go with them.
            for entry in child_spans(text, *span):
                name = parse_sexpr(text[entry[0]:entry[1]])[1]
                if name in dropped_libs - kept_libs:
                    cuts.append(entry)
        elif head == "symbol_instances":
            # KiCad 6 keeps references in a root table keyed "/<sheet>/<symbol uuid>".
            for entry in child_spans(text, *span):
                path = parse_sexpr(text[entry[0]:entry[1]])[1]
                if path.rsplit("/", 1)[-1] in project_uuids:
                    cuts.append(entry)
    return _cut(text, cuts), uuids


def _write_readonly(path: Path, body: str) -> None:
    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def prune_project(project_id: str, raw: Path = RAW) -> dict:
    target = raw / project_id
    manifest_path = target / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root_file = target / manifest["root_schematic"]
    drop = removed_mpns(root_file)
    project_uuids = {s.symbol_uuid for s in raw_symbols(root_file)
                     if pick(s.properties, MPN_FIELDS) in drop}
    removed = 0
    for path, meta in manifest["files"].items():
        if not path.endswith(".kicad_sch"):
            continue
        local = target / path
        # Decoded from bytes so CRLF files stay byte-exact outside the cut blocks.
        text = local.read_bytes().decode("utf-8")
        pruned, uuids = prune_text(text, drop, project_uuids)
        if pruned == text:
            continue
        parse_sexpr(pruned)  # the result must still be one balanced S-expression
        os.chmod(local, stat.S_IWUSR | stat.S_IRUSR)
        local.write_bytes(pruned.encode("utf-8"))
        os.chmod(local, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        body = local.read_bytes()
        # The upstream hash is kept once, from before the first prune.
        meta.setdefault("upstream_sha256", meta["sha256"])
        meta["sha256"], meta["bytes"] = hashlib.sha256(body).hexdigest(), len(body)
        removed += len(uuids)
    if removed:
        manifest["pruned"] = {
            "rule": "knowledge.parts.is_electronic",
            "removed_mpns": sorted(set(manifest.get("pruned", {}).get("removed_mpns", []))
                                   | drop)}
        _write_readonly(manifest_path,
                        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return {"project_id": project_id, "removed_mpns": len(drop), "removed_symbols": removed}


def main() -> None:
    for project in load_registry():
        print(json.dumps(prune_project(project["project_id"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
