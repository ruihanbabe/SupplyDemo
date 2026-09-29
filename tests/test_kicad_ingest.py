"""KiCad ingestion: the S-expression reader, reference resolution across both format
generations, and the BOM / AML rules — on small fixtures and on the pinned real files."""
from __future__ import annotations

import json

import pytest

from knowledge.fetch import RAW, verify
from knowledge.kicad import Placement, parse_sexpr, read_schematic
from knowledge.parts import build_parts

# ---------- S 表达式 ----------

def test_strings_atoms_and_escapes_nest_correctly():
    tree = parse_sexpr('(a (b "x \\"y\\" z") c (d))')
    assert tree == ["a", ["b", 'x "y" z'], "c", ["d"]]


def test_unbalanced_input_is_refused():
    with pytest.raises(ValueError):
        parse_sexpr("(a (b)")


# ---------- 位号解析：两代格式 ----------

def symbol(ref_prop, uuid, lib="Device:R", value="10k", extra="", instances=""):
    return (f'(symbol (lib_id "{lib}") (unit 1) (in_bom yes) (on_board yes) (uuid "{uuid}")'
            f' (property "Reference" "{ref_prop}") (property "Value" "{value}")'
            f' (property "Footprint" "R_0402") {extra} {instances})')


def test_kicad6_references_come_from_the_root_table(tmp_path):
    (tmp_path / "root.kicad_sch").write_text(
        '(kicad_sch (version 20211123) (uuid "root")'
        ' (sheet (uuid "s1") (property "Sheet name" "a") (property "Sheet file" "a.kicad_sch"))'
        ' (symbol_instances (path "/s1/u1" (reference "R7") (unit 1))))')
    (tmp_path / "a.kicad_sch").write_text(
        f'(kicad_sch (version 20211123) (uuid "a") {symbol("R?", "u1")})')
    [placed] = read_schematic(tmp_path / "root.kicad_sch")
    assert (placed.reference, placed.reference_source, placed.sheet_path) == \
        ("R7", "instance", "/s1")


def test_kicad9_a_reused_subsheet_yields_one_part_per_instance(tmp_path):
    inst = ('(instances (project "p" (path "/root/s1" (reference "R1") (unit 1))'
            ' (path "/root/s2" (reference "R2") (unit 1))))')
    (tmp_path / "root.kicad_sch").write_text(
        '(kicad_sch (version 20250114) (uuid "root")'
        ' (sheet (uuid "s1") (property "Sheetname" "a") (property "Sheetfile" "ch.kicad_sch"))'
        ' (sheet (uuid "s2") (property "Sheetname" "b") (property "Sheetfile" "ch.kicad_sch")))')
    (tmp_path / "ch.kicad_sch").write_text(
        f'(kicad_sch (version 20250114) (uuid "ch") {symbol("R1", "u1", instances=inst)})')
    placed = read_schematic(tmp_path / "root.kicad_sch")
    assert [(p.reference, p.sheet_path) for p in placed] == [("R1", "/root/s1"),
                                                             ("R2", "/root/s2")]


def test_power_symbols_are_dropped_and_units_merge(tmp_path):
    inst = lambda ref: f'(instances (project "p" (path "/root" (reference "{ref}") (unit 1))))'
    body = (symbol("#PWR01", "p1", lib="power:GND", instances=inst("#PWR01"))
            + symbol("U1", "a", lib="Amp:Dual", instances=inst("U1"))
            + symbol("U1", "b", lib="Amp:Dual", instances=inst("U1")).replace(
                "(unit 1)", "(unit 2)", 1))
    (tmp_path / "root.kicad_sch").write_text(
        f'(kicad_sch (version 20250114) (uuid "root") {body})')
    [amp] = read_schematic(tmp_path / "root.kicad_sch")
    assert (amp.reference, amp.units) == ("U1", (1, 2))


# ---------- 料号表 ----------

def placed(ref, value="10k", mpn="RC0402", dnp=False, in_bom=True, **props):
    base = {"Manufacturer": "Yageo", "MPN": mpn} if mpn else {}
    return Placement(reference=ref, sheet_path="/", sheet_file="a.kicad_sch", lib_id="Device:R",
                     symbol_uuid=ref, value=value, footprint="R_0402", in_bom=in_bom,
                     on_board=True, dnp=dnp, properties={**base, **props})


def test_one_row_per_part_number_with_its_quantity():
    [part] = build_parts([placed("R1"), placed("R2"), placed("R3", value="10K")])
    assert (part.mpn, part.manufacturer, part.quantity) == ("RC0402", "Yageo", 3)
    # Differing values are kept side by side, not reconciled.
    assert (part.designators, part.values) == (["R1", "R2", "R3"], ["10k", "10K"])


def test_unfitted_unnumbered_and_non_bom_parts_are_not_bought():
    parts = build_parts([placed("R1", dnp=True), placed("R2", value="DNP"),
                         placed("R3", DNP="DNP"), placed("FID1", mpn=None),
                         placed("TP1", in_bom=False)])
    assert parts == []


def test_the_alternates_note_is_kept_verbatim():
    [part] = build_parts([placed("C1", Substitution="Murata GRM1555C1H220JA01D")])
    assert part.alternates == "Murata GRM1555C1H220JA01D"


def test_one_part_number_with_two_makers_keeps_both_rows():
    parts = build_parts([placed("R1"), placed("R2", Manufacturer="Other")])
    assert [(p.manufacturer, p.designators) for p in parts] == [("Other", ["R2"]),
                                                                ("Yageo", ["R1"])]


# ---------- 真实数据（已钉死提交，离线可跑） ----------

PROJECTS = ("hackrf-one", "bms-c1")


@pytest.mark.parametrize("project_id", PROJECTS)
def test_raw_files_still_match_their_manifest(project_id):
    assert verify(project_id) == []


@pytest.mark.parametrize("project_id", PROJECTS)
def test_every_reference_resolved_through_an_instance_table(project_id):
    manifest = json.loads((RAW / project_id / "MANIFEST.json").read_text())
    placements = read_schematic(RAW / project_id / manifest["root_schematic"])
    assert placements and all(p.reference_source == "instance" for p in placements)


@pytest.mark.parametrize("project_id", PROJECTS)
def test_raw_layer_is_read_only(project_id):
    for path in (RAW / project_id).rglob("*"):
        if path.is_file():
            assert not path.stat().st_mode & 0o222, path
