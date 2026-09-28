"""KiCad ingestion: the S-expression reader, reference resolution across both format
generations, and the BOM / AML rules — on small fixtures and on the pinned real files."""
from __future__ import annotations

import json

import pytest

from knowledge.bom import NORMALIZED, build_bom, parse_alternates
from knowledge.fetch import RAW, verify
from knowledge.kicad import Placement, parse_sexpr, read_schematic

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


# ---------- 替代说明 ----------

def test_any_equivalent_is_a_permission_not_a_part():
    [alt] = parse_alternates("any equivalent")
    assert (alt.kind, alt.mpn) == ("any_equivalent", None)


def test_named_alternates_keep_their_manufacturer_and_bare_ones_do_not_guess():
    alts = parse_alternates("Diodes Inc. BAT54LP-7, MAX2837ETM+")
    assert [(a.manufacturer, a.mpn) for a in alts] == [("Diodes Inc.", "BAT54LP-7"),
                                                       (None, "MAX2837ETM+")]


def test_wildcards_are_patterns():
    [alt] = parse_alternates("SIT1602B*-2*-33E-60.000000*")
    assert alt.kind == "designer_pattern"


# ---------- BOM 归并 ----------

def placed(ref, value="10k", mpn="RC0402", dnp=False, lib="Device:R", in_bom=True, **props):
    base = {"Manufacturer": "Yageo", "MPN": mpn} if mpn else {}
    return Placement(reference=ref, sheet_path="/", sheet_file="a.kicad_sch", lib_id=lib,
                     symbol_uuid=ref, value=value, footprint="R_0402", in_bom=in_bom,
                     on_board=True, dnp=dnp, properties={**base, **props})


def test_same_part_groups_and_dnp_stays_a_separate_line():
    bom = build_bom([placed("R1"), placed("R10"), placed("R2", dnp=True),
                     placed("R3", value="DNP"), placed("R4", DNP="DNP")])
    fitted, *dnp = bom
    assert [p.reference for p in fitted.placements] == ["R1", "R10"]
    assert fitted.dnp is False
    assert len(dnp) == 1 and [p.reference for p in dnp[0].placements] == ["R2", "R3", "R4"]


def test_parts_without_a_number_are_told_apart_from_board_features():
    bom = build_bom([placed("FID1", value="Fiducial", mpn=None, lib="Mechanical:Fiducial"),
                     placed("P3", value="GND", mpn=None, lib="Conn:Header")])
    assert {line.placements[0].reference: line.flags for line in bom} == {
        "FID1": {"board_feature"}, "P3": {"mpn_missing"}}


def test_not_in_bom_parts_never_reach_the_bom_and_supplier_skus_ride_along():
    bom = build_bom([placed("TP1", in_bom=False), placed("C1", LCSC="C296717")])
    assert [line.placements[0].reference for line in bom] == ["C1"]
    assert bom[0].suppliers == {"lcsc": "C296717"}


# ---------- 真实数据（已钉死提交，离线可跑） ----------

PROJECTS = ("hackrf-one", "jetson-agx-thor-baseboard", "bms-c1")


@pytest.mark.parametrize("project_id", PROJECTS)
def test_raw_files_still_match_their_manifest(project_id):
    assert verify(project_id) == []


@pytest.mark.parametrize("project_id", PROJECTS)
def test_every_reference_resolved_through_an_instance_table(project_id):
    manifest = json.loads((RAW / project_id / "MANIFEST.json").read_text())
    placements = read_schematic(RAW / project_id / manifest["root_schematic"])
    assert placements and all(p.reference_source == "instance" for p in placements)


def test_the_real_projects_produce_the_expected_tables():
    meta = {pid: json.loads((NORMALIZED / pid / "meta.json").read_text()) for pid in PROJECTS}
    assert (meta["hackrf-one"]["bom_items"], meta["hackrf-one"]["items_with_alternates"]) \
        == (87, 38)
    assert meta["jetson-agx-thor-baseboard"]["items_without_mpn"] == 0
    assert meta["bms-c1"]["distinct_mpns"] == 62
    aml = (NORMALIZED / "hackrf-one" / "aml.csv").read_text()
    assert "Murata,GRM1555C1H220JA01D" in aml


def test_normalized_output_is_reproducible(tmp_path):
    from knowledge.bom import write_project

    write_project("hackrf-one", out=tmp_path)
    for name in ("bom.csv", "aml.csv", "placements.csv", "meta.json"):
        assert (tmp_path / "hackrf-one" / name).read_bytes() == \
            (NORMALIZED / "hackrf-one" / name).read_bytes(), name


def test_raw_layer_is_read_only():
    for path in (RAW / "hackrf-one").rglob("*"):
        if path.is_file():
            assert not path.stat().st_mode & 0o222, path
