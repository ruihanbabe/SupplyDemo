"""Read placed components out of a KiCad schematic hierarchy. No model, no KiCad install.

A `.kicad_sch` file is an S-expression text file, so this is plain parsing. Two format
generations matter:

- KiCad 6 (version 2021xxxx): the root file carries a `symbol_instances` table keyed by
  "/<sheet uuid>/<symbol uuid>"; the `Reference` property inside a sub-sheet may be stale.
- KiCad 7+ (version 2023xxxx on): each symbol carries its own `instances`, keyed by the
  sheet path "/<root uuid>/<sheet uuid>".

In both, the authoritative reference comes from the instance table, not the property:
a sub-sheet used twice has two references for one symbol. Power symbols and flags
(`#PWR01`, `#FLG01`) are not components and are dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Node = list[Any]


def parse_sexpr(text: str) -> Node:
    """Tokenise and nest in one pass. Quoted strings and bare atoms both become str."""
    stack: list[Node] = [[]]
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            stack.append([])
            i += 1
        elif ch == ")":
            done = stack.pop()
            stack[-1].append(done)
            i += 1
        elif ch == '"':
            i += 1
            out = []
            while text[i] != '"':
                if text[i] == "\\":
                    i += 1
                    out.append({"n": "\n", "t": "\t"}.get(text[i], text[i]))
                else:
                    out.append(text[i])
                i += 1
            stack[-1].append("".join(out))
            i += 1
        elif ch.isspace():
            i += 1
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in '()"':
                i += 1
            stack[-1].append(text[start:i])
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("unbalanced S-expression")
    return stack[0][0]


def children(node: Node, name: str) -> list[Node]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def child_value(node: Node, name: str, default: Any = None) -> Any:
    found = children(node, name)
    return found[0][1] if found and len(found[0]) > 1 else default


def properties(node: Node) -> dict[str, str]:
    return {c[1]: c[2] for c in children(node, "property") if len(c) > 2}


@dataclass(frozen=True)
class Placement:
    """One component on the board, as the schematic states it."""

    reference: str
    sheet_path: str
    sheet_file: str
    lib_id: str
    symbol_uuid: str
    value: str
    footprint: str
    in_bom: bool
    on_board: bool
    dnp: bool
    #: Every property exactly as written, including ones this parser does not interpret.
    properties: dict[str, str] = field(default_factory=dict)
    #: "instance" when resolved through the instance table, "property" when it fell back
    #: to the Reference field — the latter can be wrong in a reused sub-sheet.
    reference_source: str = "instance"
    units: tuple[int, ...] = (1,)


@dataclass
class _Sheet:
    uuid: str
    symbols: list[Node]
    subsheets: list[tuple[str, str, dict[str, str]]]  # (uuid, file, properties)
    symbol_instances: dict[str, str]


@dataclass(frozen=True)
class SheetVisit:
    """One use of a sheet file in the hierarchy. A file used twice is visited twice."""

    file: Path
    sheet_path: str
    parent_path: str | None
    sheet_uuid: str | None
    #: The sheet block's properties in the parent (Sheetname, Sheetfile, ...), verbatim.
    properties: dict[str, str]
    sheet: _Sheet


@dataclass(frozen=True)
class RawSymbol:
    """A symbol exactly as one sheet visit shows it: nothing merged, dropped or renamed."""

    sheet_path: str
    file: str
    symbol_uuid: str
    lib_id: str
    unit: int | None
    reference: str | None
    reference_source: str
    #: Single-valued flags as written: in_bom, on_board, dnp, exclude_from_sim, ...
    attributes: dict[str, str]
    properties: dict[str, str]


#: Symbol-level flags whose value is a single token. Absent ones are not invented.
ATTRIBUTES = ("in_bom", "on_board", "dnp", "exclude_from_sim", "exclude_from_board",
              "fields_autoplaced", "mirror", "convert")


def _yes(node: Node, name: str, default: bool) -> bool:
    value = child_value(node, name)
    return default if value is None else value == "yes"


def _load(path: Path) -> _Sheet:
    tree = parse_sexpr(path.read_text(encoding="utf-8"))
    if tree[0] != "kicad_sch":
        raise ValueError(f"{path.name} is not a KiCad schematic")
    subsheets = []
    for sheet in children(tree, "sheet"):
        props = properties(sheet)
        sheet_file = props.get("Sheetfile") or props.get("Sheet file")
        if not sheet_file:
            raise ValueError(f"{path.name}: sheet {child_value(sheet, 'uuid')} names no file")
        subsheets.append((child_value(sheet, "uuid"), sheet_file, props))
    instances = {}
    for table in children(tree, "symbol_instances"):
        for entry in children(table, "path"):
            instances[entry[1]] = child_value(entry, "reference")
    return _Sheet(uuid=child_value(tree, "uuid", ""), symbols=children(tree, "symbol"),
                  subsheets=subsheets, symbol_instances=instances)


def _reference(symbol: Node, sheet_path: str, legacy: dict[str, str]) -> tuple[str, str]:
    uuid = child_value(symbol, "uuid")
    if legacy:
        found = legacy.get(f"{sheet_path}/{uuid}")
        if found:
            return found, "instance"
    for instances in children(symbol, "instances"):
        for project in children(instances, "project"):
            for entry in children(project, "path"):
                if entry[1] == sheet_path:
                    return child_value(entry, "reference"), "instance"
    return properties(symbol).get("Reference", "?"), "property"


def walk(root_file: Path) -> tuple[list[SheetVisit], dict[str, str]]:
    """Every sheet visit under `root_file`, root first, plus the KiCad 6 instance table."""
    root = _load(root_file)
    legacy = root.symbol_instances
    # KiCad 6 paths start below the root ("/<sheet>"); 7+ include the root uuid.
    base = "" if legacy else f"/{root.uuid}"
    cache: dict[Path, _Sheet] = {root_file: root}
    visits = []
    queue: list[tuple[Path, str, str | None, str | None, dict[str, str]]] = [
        (root_file, base, None, None, {})]
    while queue:
        path, sheet_path, parent, sheet_uuid, props = queue.pop(0)
        if path not in cache:
            cache[path] = _load(path)
        sheet = cache[path]
        visits.append(SheetVisit(path, sheet_path or "/", parent, sheet_uuid, props, sheet))
        for sub_uuid, sub_file, sub_props in sheet.subsheets:
            queue.append((path.parent / sub_file, f"{sheet_path}/{sub_uuid}",
                          sheet_path or "/", sub_uuid, sub_props))
    return visits, legacy


def raw_symbols(root_file: Path) -> list[RawSymbol]:
    """Every symbol in every sheet visit, power symbols and extra units included."""
    visits, legacy = walk(root_file)
    found = []
    for visit in visits:
        instance_path = "" if visit.sheet_path == "/" and legacy else visit.sheet_path
        for symbol in visit.sheet.symbols:
            reference, source = _reference(symbol, instance_path, legacy)
            unit = child_value(symbol, "unit")
            found.append(RawSymbol(
                sheet_path=visit.sheet_path,
                file=visit.file.relative_to(root_file.parent).as_posix(),
                symbol_uuid=child_value(symbol, "uuid", ""),
                lib_id=child_value(symbol, "lib_id", ""),
                unit=int(unit) if unit is not None else None,
                reference=None if source == "property" and reference == "?" else reference,
                reference_source=source,
                attributes={name: child_value(symbol, name) for name in ATTRIBUTES
                            if children(symbol, name)},
                properties=properties(symbol)))
    return found


def read_schematic(root_file: Path) -> list[Placement]:
    """Every placed component in the hierarchy under `root_file`, one per reference."""
    visits, legacy = walk(root_file)
    found: dict[str, Placement] = {}
    for visit in visits:
        path = visit.file
        sheet_path = "" if visit.sheet_path == "/" and legacy else visit.sheet_path
        for symbol in visit.sheet.symbols:
            lib_id = child_value(symbol, "lib_id", "")
            reference, source = _reference(symbol, sheet_path, legacy)
            if lib_id.startswith("power:") or reference.startswith("#"):
                continue
            unit = int(child_value(symbol, "unit", 1))
            if reference in found:
                # Another unit of a multi-unit part (op-amp A/B): same component.
                prior = found[reference]
                found[reference] = Placement(**{**prior.__dict__,
                                                "units": tuple(sorted({*prior.units, unit}))})
                continue
            props = properties(symbol)
            found[reference] = Placement(
                reference=reference, sheet_path=sheet_path or "/",
                sheet_file=path.relative_to(root_file.parent).as_posix(),
                lib_id=lib_id, symbol_uuid=child_value(symbol, "uuid", ""),
                value=props.get("Value", ""), footprint=props.get("Footprint", ""),
                in_bom=_yes(symbol, "in_bom", True), on_board=_yes(symbol, "on_board", True),
                dnp=_yes(symbol, "dnp", False), properties=props,
                reference_source=source, units=(unit,))
    return sorted(found.values(), key=lambda p: _natural(p.reference))


def _natural(reference: str) -> tuple[str, int, str]:
    """R2 before R10."""
    head = reference.rstrip("0123456789")
    digits = reference[len(head):]
    return (head, int(digits) if digits else -1, reference)
