"""Turn placements into the two tables an EMS or buyer expects: a BOM and an AML.

BOM (bill of materials): one line item per distinct part, with quantity and the list of
reference designators. Do-not-place parts stay listed on their own lines with DNP=Y, as
assembly houses expect, rather than disappearing.

AML (approved manufacturer list): for each line item, the primary part plus every
alternate the designer declared. The alternate text is kept verbatim next to its parsed
form, because "any equivalent" and "Murata GRM1555C1H220JA01D" are different kinds of
permission and the difference must survive into the business rules.

Field names differ per project ("MPN", "Part Number", "PartNumber"). The alias table
below is the only place that knows this — the adapter layer, not the business logic.

Run with: make normalize-hardware
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from knowledge.fetch import RAW, ROOT
from knowledge.kicad import Placement, _natural, read_schematic

NORMALIZED = ROOT / "data" / "hardware" / "normalized"

#: Canonical field -> property names seen in the wild, compared case-insensitively.
FIELD_ALIASES = {
    "mpn": ("mpn", "part number", "partnumber", "manufacturer part number",
            "mfr part number", "mfr_pn", "manf#"),
    "manufacturer": ("manufacturer", "mfr", "manufacturer_name", "manf"),
    "description": ("description",),
    "alternates": ("substitution", "alternative", "alternates"),
    "dnp_marker": ("dnp", "dnm"),
}
#: Supplier stock-keeping numbers carried in the schematic, keyed by supplier.
SUPPLIER_FIELDS = {"lcsc": ("lcsc",), "digikey": ("digikey", "digi-key_pn"),
                   "mouser": ("mouser",)}
#: Parametric fields worth carrying; the rest stay in the placement's raw properties.
PARAMETER_FIELDS = ("voltage", "dielectric", "tolerance", "current", "power", "isat",
                    "imax", "size", "color", "remarks")
EMPTY = {"", "~"}
#: Values some designers use instead of a do-not-place attribute.
DNP_VALUES = {"DNP", "DNM"}
#: Libraries whose symbols are copper or silkscreen features, not parts anyone buys.
BOARD_FEATURE_LIBRARIES = ("Fiducial", "MountingHole", "NetTie", "TestPoint", "Symbol")
#: Reference-designator prefixes conventionally used for the same features.
BOARD_FEATURE_PREFIXES = {"FID", "LOGO", "MH", "H", "TP", "NT"}

BOM_COLUMNS = ("Item", "Qty", "Reference Designators", "Value", "Description", "Footprint",
               "Manufacturer", "Manufacturer Part Number", "Supplier Part Numbers",
               "Parameters", "DNP", "Alternates", "Flags")
AML_COLUMNS = ("Item", "Rank", "Kind", "Manufacturer", "Manufacturer Part Number",
               "Source Text")
PLACEMENT_COLUMNS = ("Reference", "Item", "Sheet File", "Sheet Path", "Lib ID", "Value",
                     "Footprint", "Manufacturer", "Manufacturer Part Number", "DNP",
                     "Symbol UUID")


def pick(props: dict[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {key.lower(): value for key, value in props.items()}
    for name in names:
        value = lowered.get(name)
        if value is not None and value.strip() not in EMPTY:
            return value.strip()
    return None


def is_dnp(placement: Placement) -> bool:
    return (placement.dnp or placement.value.strip().upper() in DNP_VALUES
            or pick(placement.properties, FIELD_ALIASES["dnp_marker"]) is not None)


def is_board_feature(placement: Placement) -> bool:
    """Judged from the footprint library too: projects keep such symbols in their own
    symbol library ("LibreSolar:Fiducial") but take the footprint from KiCad's."""
    libraries = (placement.lib_id.split(":", 1)[0], placement.footprint.split(":", 1)[0])
    prefix = placement.reference.rstrip("0123456789")
    return (prefix in BOARD_FEATURE_PREFIXES
            or any(lib.startswith(BOARD_FEATURE_LIBRARIES) for lib in libraries))


def parameters(props: dict[str, str]) -> str:
    lowered = {key.lower(): (key, value) for key, value in props.items()}
    found = []
    for name in PARAMETER_FIELDS:
        key, value = lowered.get(name, (None, None))
        # Library templates leave placeholders such as "template_dielectric": unknown,
        # not a value.
        if value and value.strip() not in EMPTY and not value.startswith("template_"):
            found.append(f"{key}={value.strip()}")
    return "; ".join(found)


@dataclass
class Alternate:
    kind: str  # designer_specified | designer_pattern | any_equivalent
    manufacturer: str | None
    mpn: str | None
    source_text: str


def parse_alternates(text: str) -> list[Alternate]:
    """Split a designer's substitution note into entries, without guessing.

    "Murata GRM1555C1H220JA01D" names a manufacturer; "MAX2837ETM+" does not, and its
    manufacturer stays unknown rather than being copied from the primary part.
    """
    if re.fullmatch(r"\s*(any\s+)?equivalent\s*", text, re.IGNORECASE):
        return [Alternate("any_equivalent", None, None, text)]
    entries = []
    for chunk in re.split(r"[,;]", text):
        entry = chunk.strip()
        if not entry:
            continue
        head, _, last = entry.rpartition(" ")
        manufacturer, mpn = (head.strip() or None, last) if head else (None, entry)
        kind = "designer_pattern" if "*" in mpn else "designer_specified"
        entries.append(Alternate(kind, manufacturer, mpn, text))
    return entries


@dataclass
class LineItem:
    item: int
    manufacturer: str | None
    mpn: str | None
    value: str
    description: str | None
    footprint: str
    dnp: bool
    placements: list[Placement] = field(default_factory=list)
    suppliers: dict[str, str] = field(default_factory=dict)
    parameters: str = ""
    alternates: list[Alternate] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)


def build_bom(placements: list[Placement]) -> list[LineItem]:
    groups: dict[tuple, LineItem] = {}
    for placement in placements:
        if not placement.in_bom:
            continue
        props = placement.properties
        mpn = pick(props, FIELD_ALIASES["mpn"])
        manufacturer = pick(props, FIELD_ALIASES["manufacturer"])
        dnp = is_dnp(placement)
        key = (("mpn", (manufacturer or "").casefold(), mpn, dnp) if mpn else
               ("generic", placement.value, placement.footprint, placement.lib_id, dnp))
        line = groups.get(key)
        if line is None:
            line = groups[key] = LineItem(
                item=0, manufacturer=manufacturer, mpn=mpn, value=placement.value,
                description=pick(props, FIELD_ALIASES["description"]),
                footprint=placement.footprint, dnp=dnp, parameters=parameters(props))
            if not mpn:
                line.flags.add("board_feature" if is_board_feature(placement)
                               else "mpn_missing")
            note = pick(props, FIELD_ALIASES["alternates"])
            if note:
                line.alternates = parse_alternates(note)
        elif placement.value != line.value:
            line.flags.add("value_differs_within_item")
        for supplier, names in SUPPLIER_FIELDS.items():
            sku = pick(props, names)
            if sku:
                if line.suppliers.get(supplier, sku) != sku:
                    line.flags.add(f"{supplier}_sku_differs_within_item")
                line.suppliers.setdefault(supplier, sku)
        line.placements.append(placement)

    ordered = sorted(groups.values(),
                     key=lambda line: (line.dnp, _natural(line.placements[0].reference)))
    for number, line in enumerate(ordered, start=1):
        line.item = number
    return ordered


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_project(project_id: str, raw: Path = RAW, out: Path = NORMALIZED) -> dict:
    source = raw / project_id
    manifest_bytes = (source / "MANIFEST.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    placements = read_schematic(source / manifest["root_schematic"])
    bom = build_bom(placements)
    target = out / project_id
    target.mkdir(parents=True, exist_ok=True)

    item_of = {p.reference: line.item for line in bom for p in line.placements}
    _write_csv(target / "bom.csv", BOM_COLUMNS, [{
        "Item": line.item, "Qty": len(line.placements),
        "Reference Designators": ",".join(p.reference for p in line.placements),
        "Value": line.value, "Description": line.description or "",
        "Footprint": line.footprint, "Manufacturer": line.manufacturer or "",
        "Manufacturer Part Number": line.mpn or "",
        "Supplier Part Numbers": "; ".join(f"{k}:{v}" for k, v in sorted(line.suppliers.items())),
        "Parameters": line.parameters, "DNP": "Y" if line.dnp else "",
        "Alternates": len(line.alternates), "Flags": ",".join(sorted(line.flags)),
    } for line in bom])
    aml = []
    for line in bom:
        if line.mpn:
            aml.append({"Item": line.item, "Rank": 1, "Kind": "primary",
                        "Manufacturer": line.manufacturer or "",
                        "Manufacturer Part Number": line.mpn, "Source Text": ""})
        for rank, alt in enumerate(line.alternates, start=2):
            aml.append({"Item": line.item, "Rank": rank, "Kind": alt.kind,
                        "Manufacturer": alt.manufacturer or "",
                        "Manufacturer Part Number": alt.mpn or "",
                        "Source Text": alt.source_text})
    _write_csv(target / "aml.csv", AML_COLUMNS, aml)
    _write_csv(target / "placements.csv", PLACEMENT_COLUMNS, [{
        "Reference": p.reference, "Item": item_of.get(p.reference, ""),
        "Sheet File": p.sheet_file, "Sheet Path": p.sheet_path, "Lib ID": p.lib_id,
        "Value": p.value, "Footprint": p.footprint,
        "Manufacturer": pick(p.properties, FIELD_ALIASES["manufacturer"]) or "",
        "Manufacturer Part Number": pick(p.properties, FIELD_ALIASES["mpn"]) or "",
        "DNP": "Y" if is_dnp(p) else "", "Symbol UUID": p.symbol_uuid,
    } for p in placements if p.in_bom])

    summary = {
        "project_id": project_id, "repo": manifest["repo"], "commit": manifest["commit"],
        "license": manifest["license"],
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "placements": len(placements),
        "excluded_not_in_bom": sum(not p.in_bom for p in placements),
        "bom_items": len(bom), "dnp_items": sum(line.dnp for line in bom),
        "distinct_mpns": len({line.mpn for line in bom if line.mpn}),
        "items_without_mpn": sum(not line.mpn for line in bom),
        "items_with_alternates": sum(bool(line.alternates) for line in bom),
        "aml_rows": len(aml),
    }
    (target / "meta.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
    return summary


def main() -> None:
    from knowledge.fetch import load_registry

    for project in load_registry():
        print(json.dumps(write_project(project["project_id"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
