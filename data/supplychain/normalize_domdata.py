#!/usr/bin/env python3
"""把 domdata/ 的原始 CSV 规范化为长格式三表。

原始层只读：本脚本不写 domdata/ 的源文件，只读取并在 normalized/ 产出。
宽表里重复的 (Manufacturer, MPN) 列组按位置展开为 candidate 行，
避免 DictReader 静默丢弃重复列名。
"""
from __future__ import annotations

import csv
import hashlib
import json
import pathlib

RAW = pathlib.Path(__file__).parent / "domdata"
OUT = pathlib.Path(__file__).parent / "normalized"

DISTRIBUTORS = {"digikey": "Digikey", "mouser": "Mouser", "rs": "RS",
                "newark": "Newark", "farnell": "Farnell"}

KICAD_BOMS = ["ArdPromSD", "Glasgow_revC3", "PCB-Stimulator", "hbridge_driver",
               "Spikeling-V2"]

LINE_FIELDS = ["project_id", "line_id", "source_row_index",
               "reference", "quantity", "description"]

# 源表订正：原始层保持对源忠实不改，订正只在本层生效，每条须有外部依据。
# key = (project_id, source_row_index)，value = 覆盖的字段。
CORRECTIONS = {
    # README: "12 proxy LED indicate continuously which mode is currently being set."
    # 源表 reference 末项写作 "13"（漏 d 前缀），且 qty 记为 11。
    ("Spikeling-V2", 5): {
        "reference": "d2,d3,d4,d5,d6,d7,d8,d9,d10,d11,d12,d13",
        "quantity": "12",
        "_why": "README 载明 12 颗 proxy LED；源表位号缺前缀且数量少计 1",
    },
    # README: "2 ADC 8bit expander chips"；芯片座同为 2 个，可互证。
    ("Spikeling-V2", 15): {
        "quantity": "2",
        "_why": "README 载明 2 片 ADC；源表 2 个位号却记 qty=1",
    },
    # DIP 芯片座焊在 u2/u3 焊盘上，MCP3208 插入其中；源表漏填位号。
    ("Spikeling-V2", 16): {
        "reference": "u2,u3",
        "_why": "芯片座占用 u2/u3 焊盘，源表该行位号字段为空",
    },
}
# 厂商别名归一：源 BOM 里同一厂商有多种写法，直接去重会把同一元件拆成多条。
# 取值一律取源数据中已出现的较完整写法，不引入源数据里没有的名称。
# 全量核查见 normalized/projects.yaml 的 manufacturer_aliases 段。
MANUFACTURER_ALIASES = {
    "Bivar": "Bivar Inc",
    "TE": "TE Connectivity",
    "Microchip": "Microchip Technology",
    "Murata": "Murata Electronics",
    "Samtec": "Samtec Inc.",
}

CAND_FIELDS = ["line_id", "candidate_seq", "component_id",
               "manufacturer", "manufacturer_raw", "mpn"]
COMPONENT_FIELDS = ["component_id", "manufacturer", "mpn", "identity_status", "used_by_count"]
SKU_FIELDS = ["line_id", "distributor", "distributor_sku"]


def component_id(manufacturer: str, mpn: str) -> str:
    """跨项目稳定的元件标识：精确 trim 后的厂商+型号，不做别名归并或标点清洗。

    与 component-library/build_library.py 的 uid() 同算法，便于两侧结果互相校验。
    """
    key = json.dumps([manufacturer or None, mpn], ensure_ascii=False)
    return "part_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def parse_kicad_bom(project_id: str, path: pathlib.Path):
    rows = list(csv.reader(path.open(encoding="utf-8-sig")))
    header, body = rows[0], rows[1:]
    idx = {"References": None, "Qty": None, "Description": None}
    for i, col in enumerate(header):
        if col in idx and idx[col] is None:
            idx[col] = i
    cand_pairs = [(i, i + 1) for i, c in enumerate(header)
                  if c == "Manufacturer" and i + 1 < len(header) and header[i + 1] == "MPN"]
    dist_cols = {i: key for key, label in DISTRIBUTORS.items()
                 for i, c in enumerate(header) if c == label}

    lines, cands, skus = [], [], []
    seq_no = 0
    for n, row in enumerate(body, start=2):
        if not any(cell.strip() for cell in row):
            continue
        seq_no += 1
        line_id = f"{project_id}-{seq_no:03d}"
        rec = {
            "project_id": project_id, "line_id": line_id, "source_row_index": n,
            "reference": row[idx["References"]].strip(),
            "quantity": row[idx["Qty"]].strip(),
            "description": row[idx["Description"]].strip(),
        }
        fix = CORRECTIONS.get((project_id, n))
        if fix:
            rec.update({k: v for k, v in fix.items() if not k.startswith("_")})
        lines.append(rec)
        c_seq = 0
        for mi, pi in cand_pairs:
            man = row[mi].strip() if mi < len(row) else ""
            mpn = row[pi].strip() if pi < len(row) else ""
            if not (man or mpn):
                continue
            c_seq += 1
            canon = MANUFACTURER_ALIASES.get(man, man)
            cands.append({"line_id": line_id, "candidate_seq": c_seq,
                          "component_id": component_id(canon, mpn) if mpn else "",
                          "manufacturer": canon, "manufacturer_raw": man,
                          "mpn": mpn})
        for ci, dist in dist_cols.items():
            val = row[ci].strip() if ci < len(row) else ""
            if val:
                skus.append({"line_id": line_id, "distributor": dist,
                             "distributor_sku": val})
    return lines, cands, skus


def write_csv(path: pathlib.Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    all_lines, all_cands, all_skus = [], [], []
    for name in KICAD_BOMS:
        l, c, s = parse_kicad_bom(name, RAW / f"{name}.csv")
        all_lines += l; all_cands += c; all_skus += s

    # 元件主表：148 次候选出现按 (manufacturer, mpn) 精确去重
    comps: dict[str, dict] = {}
    for c in all_cands:
        cid = c["component_id"]
        if not cid:
            continue
        if cid not in comps:
            comps[cid] = {"component_id": cid, "manufacturer": c["manufacturer"],
                          "mpn": c["mpn"],
                          "identity_status": "source_asserted" if c["manufacturer"]
                                             else "manufacturer_not_in_source",
                          "used_by_count": 0}
        comps[cid]["used_by_count"] += 1
    all_comps = sorted(comps.values(), key=lambda x: (x["manufacturer"], x["mpn"]))

    write_csv(OUT / "component.csv", all_comps, COMPONENT_FIELDS)
    write_csv(OUT / "bom_line.csv", all_lines, LINE_FIELDS)
    write_csv(OUT / "bom_line_candidate.csv", all_cands, CAND_FIELDS)
    write_csv(OUT / "bom_line_distributor_sku.csv", all_skus, SKU_FIELDS)

    manifest = {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                         "bytes": p.stat().st_size,
                         "lines": len(p.read_text(encoding="utf-8-sig").splitlines())}
                for p in sorted(RAW.iterdir())
                if p.is_file() and p.suffix == ".csv"}
    (RAW / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"component                 {len(all_comps):>4}")
    print(f"bom_line                  {len(all_lines):>4}")
    print(f"bom_line_candidate        {len(all_cands):>4}")
    print(f"bom_line_distributor_sku  {len(all_skus):>4}")


if __name__ == "__main__":
    main()
