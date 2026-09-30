"""文件摘要（F19 · datasheet 存储与检索）

spec §6.1：以页为单位存储；关键词 + 同义词扩展定位页码，Agent 再读该页原文；不做向量。
- DatasheetStore 接口两个实现：MemoryDatasheetStore（离线）与 PgDatasheetStore（datasheet_version / datasheet_page，迁移 0007）。
  put_version(meta, pages) 存一个版本（旧版本保留、同版本重复存报错）；versions(mpn) 按入库顺序；page / pages 取页；
  candidates(mpn, version, groups) 取可能命中的页（Pg 用全文索引预筛，内存返回全部页），最终打分统一由 search() 做。
- expand(query, synonyms, enabled)：查询拆成词组，每组按种子同义词扩展（消融 datasheet_synonyms 关闭时不扩展）。
- search(store, mpn, version, query, ...)：对候选页按"命中的词组数、再按出现次数"打分，得分降序、页码升序，返回页码与命中处片段。
  两个实现排序一致（打分在 Python 里统一做）。
依赖：sqlalchemy（Pg 实现）、标准库 re / json。被 datasheets.fetch（入库）、datasheets.server（工具）、datasheets.cite 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # JSON 字段
import re  # 词边界匹配
from typing import Protocol  # 接口

from sqlalchemy import text  # Pg 查询


class DatasheetStore(Protocol):
    """datasheet 存储接口。"""

    def put_version(self, meta: dict, pages: list[dict]) -> None: ...  # 存一个版本
    def versions(self, mpn: str) -> list[dict]: ...  # 全部版本（入库顺序）
    def page(self, mpn: str, version: str, page: int) -> str | None: ...  # 一页文本
    def pages(self, mpn: str, version: str) -> list[tuple[int, str]]: ...  # 全部页
    def candidates(self, mpn: str, version: str, groups: list[list[str]]) -> list[tuple[int, str]]: ...  # 候选页


def latest(store: DatasheetStore, mpn: str) -> dict | None:
    """最新版本的元数据（没有即 None）。"""
    vs = store.versions(mpn)  # 全部版本
    return vs[-1] if vs else None  # 最后入库的


# 功能：内存实现。依赖：无。完成：深拷贝存取；同版本重复存报错（与主键一致）。
class MemoryDatasheetStore:
    def __init__(self) -> None:
        self._versions: dict[str, list[dict]] = {}  # 料号 → 版本元数据（入库顺序）
        self._pages: dict[tuple[str, str], dict[int, str]] = {}  # (料号, 版本) → 页码 → 文本

    def put_version(self, meta, pages):
        if any(v["version"] == meta["version"] for v in self._versions.get(meta["mpn"], [])):  # 已有
            raise ValueError(f"datasheet 版本已存在：{meta['mpn']} {meta['version'][:12]}")  # 只追加
        self._versions.setdefault(meta["mpn"], []).append(json.loads(json.dumps(meta)))  # 元数据
        self._pages[(meta["mpn"], meta["version"])] = {p["page"]: p["text"] for p in pages}  # 页

    def versions(self, mpn):
        return json.loads(json.dumps(self._versions.get(mpn, [])))  # 副本

    def page(self, mpn, version, page):
        return self._pages.get((mpn, version), {}).get(page)  # 文本或空

    def pages(self, mpn, version):
        return sorted(self._pages.get((mpn, version), {}).items())  # 按页码

    def candidates(self, mpn, version, groups):
        return self.pages(mpn, version)  # 内存实现不预筛


# 功能：Postgres 实现。依赖：迁移 0007 两张表；调用方给连接（不提交）。
# 完成：候选页用 websearch_to_tsquery 预筛（词组之间 OR、多词短语加引号）；全文索引列由数据库生成。
class PgDatasheetStore:
    def __init__(self, connection) -> None:
        self.conn = connection  # 数据库连接

    def put_version(self, meta, pages):
        self.conn.execute(text("""
            INSERT INTO datasheet_version (mpn, version, source_url, title, toc, fetched_day)
            VALUES (:mpn, :version, :source_url, :title, CAST(:toc AS JSONB), :fetched_day)"""),
            {**meta, "toc": json.dumps(meta["toc"], ensure_ascii=False)})  # 版本（主键冲突即报错）
        for p in pages:  # 每页
            self.conn.execute(text("INSERT INTO datasheet_page (mpn, version, page, text) "
                                   "VALUES (:m, :v, :p, :t)"),
                              {"m": meta["mpn"], "v": meta["version"], "p": p["page"], "t": p["text"]})  # 页

    def versions(self, mpn):
        rows = self.conn.execute(text("SELECT mpn, version, source_url, title, toc, fetched_day "
                                      "FROM datasheet_version WHERE mpn = :m ORDER BY seq"),
                                 {"m": mpn}).mappings().all()  # 入库顺序
        return [dict(r) for r in rows]  # 字典列表

    def page(self, mpn, version, page):
        return self.conn.execute(text("SELECT text FROM datasheet_page WHERE mpn = :m AND version = :v "
                                      "AND page = :p"), {"m": mpn, "v": version, "p": page}).scalar()  # 文本或空

    def pages(self, mpn, version):
        rows = self.conn.execute(text("SELECT page, text FROM datasheet_page WHERE mpn = :m AND version = :v "
                                      "ORDER BY page"), {"m": mpn, "v": version}).all()  # 全部页
        return [(r[0], r[1]) for r in rows]  # 元组列表

    def candidates(self, mpn, version, groups):
        terms = [f'"{t}"' if " " in t else t for g in groups for t in g]  # 多词加引号
        rows = self.conn.execute(text("SELECT page, text FROM datasheet_page WHERE mpn = :m AND version = :v "
                                      "AND tsv @@ websearch_to_tsquery('simple', :q) ORDER BY page"),
                                 {"m": mpn, "v": version, "q": " OR ".join(terms)}).all()  # 全文预筛
        return [(r[0], r[1]) for r in rows]  # 候选页


def expand(query: str, synonyms: list[list[str]], enabled: bool = True) -> list[list[str]]:
    """查询 → 词组列表：先整体匹配同义词组（可含多词短语），剩下的词各自成组。"""
    q = query.strip().lower()  # 规范化
    groups = []  # 词组
    if enabled:  # 同义词扩展
        for syn in synonyms:  # 每组同义词
            hit = next((s for s in sorted(syn, key=len, reverse=True) if re.search(rf"\b{re.escape(s.lower())}\b", q)),
                       None)  # 查询里出现的写法（长的优先）
            if hit:  # 命中
                groups.append([s.lower() for s in syn])  # 整组
                q = re.sub(rf"\b{re.escape(hit.lower())}\b", " ", q)  # 从查询里拿掉
    groups += [[w] for w in q.split() if w]  # 剩下的词
    return groups  # 词组


def _pattern(term: str) -> str:
    """词边界上的匹配（不跨进别的词：IL 不会命中 applications）。"""
    return rf"(?<![\w]){re.escape(term)}(?![\w])"  # 正则


def _count(term: str, body: str) -> int:
    """词边界上的出现次数（调用方已转小写）。"""
    return len(re.findall(_pattern(term), body))  # 计数


def search(store: DatasheetStore, mpn: str, version: str, query: str, *, synonyms: list[list[str]],
           use_synonyms: bool = True, limit: int = 5, snippet_chars: int = 80) -> list[dict]:
    """检索：返回 [{page, score, matched, snippet}]，按（命中词组数, 出现次数）降序、页码升序。"""
    groups = expand(query, synonyms, use_synonyms)  # 词组
    hits = []  # 结果
    for page, body in store.candidates(mpn, version, groups):  # 候选页
        low = body.lower()  # 小写
        counts = [max((_count(t, low) for t in g), default=0) for g in groups]  # 每组最好的写法
        matched = sum(1 for c in counts if c)  # 命中的词组数
        if not matched:  # 一个都没中
            continue  # 跳过
        first = min(m.start() for g in groups for t in g for m in re.finditer(_pattern(t), low))  # 第一处命中（同一规则）
        start = max(0, first - snippet_chars // 2)  # 片段起点
        hits.append({"page": page, "score": [matched, sum(counts)],  # 得分
                     "matched": matched, "snippet": body[start:start + snippet_chars].replace("\n", " ")})  # 片段
    hits.sort(key=lambda h: (-h["score"][0], -h["score"][1], h["page"]))  # 排序
    return hits[:limit]  # 截断
