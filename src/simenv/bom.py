"""文件摘要（F03 环境内核 · BOM 读取）

本文件从只读原始层原理图读出两块板的 BOM（料号 → 单板用量），供生成环境用。
- load_boms(raw)：先做 MANIFEST 哈希校验，再走与 `make build-parts` 同一条解析路径（knowledge.kicad + knowledge.parts），
  所以离线可用、与 hw_part 表内容一致，且不需要连数据库。
- 同一料号两个厂商（hw_part 分两行）在这里按料号合并用量：库存按料号全局共享（spec §1.7 ④）。
依赖：knowledge.fetch（登记表、原始层路径、校验）、knowledge.kicad（解析原理图）、knowledge.parts（归并料号）。
被 seed.generate 与 cli 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 读 MANIFEST
from pathlib import Path  # 路径类型

from knowledge.fetch import RAW, load_registry, verify  # 原始层路径、项目登记表、哈希校验
from knowledge.kicad import read_schematic  # 解析 KiCad 原理图得到贴装位
from knowledge.parts import build_parts  # 贴装位归并为料号


# ---------------------------------------------------------------------------
# load_boms：读出所有登记项目的 BOM
# 功能：返回 {project_id: {mpn: 单板用量}}；文件与 MANIFEST 不符时拒绝。
# 依赖：见文件摘要；被 seed 与 cli 调用。
# 完成：校验、解析、按料号合并；按登记表顺序返回（字典有序，保证生成可复现）。
# ---------------------------------------------------------------------------
def load_boms(raw: Path = RAW) -> dict[str, dict[str, int]]:
    boms: dict[str, dict[str, int]] = {}  # 结果容器
    for project in load_registry():  # 遍历 sources.yaml 登记的每块板
        pid = project["project_id"]  # 板标识
        changed = verify(pid, raw)  # 原始文件哈希校验
        if changed:  # 有文件被改过
            raise ValueError(f"{pid}: files differ from MANIFEST: {changed}")  # 拒绝使用
        manifest = json.loads((raw / pid / "MANIFEST.json").read_text(encoding="utf-8"))  # 读清单
        parts = build_parts(read_schematic(raw / pid / manifest["root_schematic"]))  # 解析得料号
        bom: dict[str, int] = {}  # 这块板的 BOM
        for part in parts:  # 每个料号（可能同料号多厂商多行）
            bom[part.mpn] = bom.get(part.mpn, 0) + part.quantity  # 按料号合并用量
        boms[pid] = dict(sorted(bom.items()))  # 按料号排序，保证顺序稳定
    return boms  # 返回全部 BOM
