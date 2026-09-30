"""文件摘要（F13 · 污点标签）

标签存放在 Artifact 的 meta.taint：{字段路径规则: 标签}。
- 路径：`.` 分层，`[i]` 为具体列表下标，`[]` 为任意下标，`*` 为缺省；查询 label_of(taint, 路径) 取最长匹配规则（按规则长度），
  没有任何匹配且没有 `*` 时按不可信处理（缺标签 = 来源不明 = 不可信）。
- weakest(...)：合并取最弱：untrusted < external < trusted = verified。
- taint_of(artifact)：取 Artifact 的标签表；mark_verified(taint, paths)：去污（F23 引用校验通过后用，产出新版本时写入）。
- top_level_untrusted(artifact)：顶层字段里标签为不可信的键（F16 摘要把它们放进引用块）。
依赖：标准库 re。被 business（打标、汇点）、runtime.dispatch（LLM 产出打标）、context.text（给人看的文本）、taint.sinks 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 路径规则匹配
from functools import lru_cache  # 规则编译缓存

TRUSTED, EXTERNAL, UNTRUSTED, VERIFIED = "trusted", "external", "untrusted", "verified"  # 四种标签
RANK = {UNTRUSTED: 0, EXTERNAL: 1, TRUSTED: 2, VERIFIED: 2}  # 强弱（数字越小越弱）
ALL_TRUSTED = {"*": TRUSTED}  # 全部可信（代码计算）
ALL_UNTRUSTED = {"*": UNTRUSTED}  # 全部不可信（LLM 产出、来源不明）


@lru_cache(maxsize=1024)
def _rule(key: str) -> re.Pattern:
    """把规则编译为正则：`[]` 匹配任意下标，规则须对齐到字段边界（后面是结尾、`.` 或 `[`）。"""
    body = re.escape(key).replace(r"\[\]", r"\[\d+\]")  # 任意下标
    return re.compile(rf"^{body}(?=$|[.\[])")  # 前缀匹配到字段边界


def label_of(taint: dict | None, path: str) -> str:
    """字段标签：最长匹配规则；都不匹配用 `*`；没有 `*` 按不可信。"""
    taint = taint or {}  # 没有标签表
    best, best_len = taint.get("*", UNTRUSTED), -1  # 缺省
    for key, label in taint.items():  # 每条规则
        if key != "*" and len(key) > best_len and _rule(key).match(path):  # 更长且匹配
            best, best_len = label, len(key)  # 采用
    return best  # 标签


def weakest(*labels: str) -> str:
    """合并取最弱（任何一个来源不可信，结果就不可信）。"""
    return min(labels, key=lambda lb: RANK[lb]) if labels else UNTRUSTED  # 最弱


def taint_of(artifact: dict | None) -> dict:
    """Artifact 的标签表（没有即空表 = 全部按不可信）。"""
    return ((artifact or {}).get("meta") or {}).get("taint") or {}  # 标签表


def mark_verified(taint: dict, paths: list[str]) -> dict:
    """去污：把指定路径标为已校验（只对不可信 / 外部字段有意义），返回新表。"""
    return {**taint, **{p: VERIFIED for p in paths}}  # 追加更长规则覆盖


def top_level_untrusted(artifact: dict) -> set[str]:
    """顶层字段中标签为不可信的键。"""
    taint = taint_of(artifact)  # 标签表
    return {k for k in artifact.get("content", {}) if label_of(taint, k) == UNTRUSTED}  # 过滤
