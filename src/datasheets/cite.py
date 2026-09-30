"""文件摘要（F19 · 引用校验器）

spec §2.4"向用户输出事实性结论"：被引用的文档版本和页码真实存在，被引用的数值出现在该页原文中；不通过的结论降级为"无法确认"。
check_citation(store, citation) → 原因列表（空 = 通过）。引用字段：mpn、version、page，另可给 value（数值，按数字规范化比较：
25 与 25.0 相同，千分位逗号忽略）与 quote（原文片段，忽略大小写与连续空白后逐字出现）。
通过后由调用方用 F13 mark_verified 对相应字段去污（F23 Verifier、F24 问答）。
依赖：datasheets.store、标准库 re / decimal。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 数字与空白
from decimal import Decimal, InvalidOperation  # 数字规范化

from datasheets.store import DatasheetStore  # 存储

NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")  # 数字（允许千分位逗号）


def _norm_num(s: str) -> Decimal | None:
    """数字字符串 → Decimal（去逗号）；不是数字返回 None。"""
    try:  # 转换
        return Decimal(str(s).replace(",", "")).normalize()  # 规范化（25.0 → 25）
    except (InvalidOperation, ValueError):  # 不是数字
        return None  # 空


def _squash(s: str) -> str:
    """小写并压缩空白。"""
    return re.sub(r"\s+", " ", s).strip().lower()  # 规范化


# 功能：见文件摘要。依赖：store.versions / page。完成：先查版本与页，再查数值与片段；原因逐条写明。
def check_citation(store: DatasheetStore, citation: dict) -> list[str]:
    mpn, version, page = citation.get("mpn"), citation.get("version"), citation.get("page")  # 引用位置
    if not any(v["version"] == version for v in store.versions(mpn)):  # 版本不存在
        return [f"{mpn} 没有版本 {str(version)[:12]}"]  # 原因
    body = store.page(mpn, version, page) if isinstance(page, int) else None  # 页文本
    if body is None:  # 页不存在
        return [f"{mpn} 版本 {version[:12]} 没有第 {page} 页"]  # 原因
    reasons = []  # 原因
    if citation.get("value") is not None:  # 引用了数值
        want = _norm_num(citation["value"])  # 规范化
        found = {_norm_num(n) for n in NUM.findall(body)}  # 该页出现的数字
        if want is None or want not in found:  # 没出现
            reasons.append(f"数值 {citation['value']} 不在 {mpn} 第 {page} 页原文中")  # 原因
    if citation.get("quote") and _squash(citation["quote"]) not in _squash(body):  # 引用了片段但不逐字出现
        reasons.append(f"引用片段不在 {mpn} 第 {page} 页原文中")  # 原因
    return reasons  # 结果
