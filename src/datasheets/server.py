"""文件摘要（F19 · datasheet Server）

spec §5.4 datasheet Server 的四个工具（全部 R0、按料号作用域 scoped——F11 可把 Verifier 限定为只读原料与该候选的 datasheet）：
- find_datasheet(mpn)：按需入库（首次进入案子才获取），返回最新版本号、页数、标题、目录条数；source=external（涉及下载）。
- datasheet_toc(mpn, version?)：目录。
- datasheet_search(mpn, query, version?)：关键词 + 同义词找页码与片段（spec §6.1）。
- datasheet_read_page(mpn, page, version?)：读某一页原文（外部内容，G6；交给模型前由 F13 做疑似指令检测）。
version 缺省为最新版本；引用时写"料号 v版本前 12 位 第 N 页"。
load_datasheets_config()：读 config/datasheets.yaml。
依赖：datasheets.store / fetch、tools.server、pydantic、PyYAML。被装配代理层的代码（F22/F23、测试）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 配置路径

import yaml  # 读配置
from pydantic import BaseModel, Field  # 参数模型

from datasheets.fetch import FetchRejected, Ingestor  # 入库
from datasheets.store import DatasheetStore, latest, search  # 存储与检索
from tools.server import NotFound, Server, ToolDef  # 工具定义

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
DATASHEETS_CONFIG = ROOT / "config" / "datasheets.yaml"  # 默认配置


def load_datasheets_config(path: Path = DATASHEETS_CONFIG) -> dict:
    """读 datasheet 配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


class FindArgs(BaseModel):
    """找 datasheet。"""
    mpn: str = Field(min_length=1)  # 料号


class TocArgs(BaseModel):
    """看目录。"""
    mpn: str = Field(min_length=1)  # 料号
    version: str | None = None  # 版本（缺省最新）


class SearchArgs(BaseModel):
    """文档内检索。"""
    mpn: str = Field(min_length=1)  # 料号
    query: str = Field(min_length=1)  # 参数名或关键词
    version: str | None = None  # 版本（缺省最新）


class PageArgs(BaseModel):
    """读某一页。"""
    mpn: str = Field(min_length=1)  # 料号
    page: int = Field(ge=1)  # 页码
    version: str | None = None  # 版本（缺省最新）


# 功能：构造 datasheet Server。依赖：存储、入库器、配置、同义词开关。完成：没入库的料读目录 / 检索 / 读页返回"查不到"并提示先 find。
def datasheet_server(store: DatasheetStore, ingestor: Ingestor, *, config: dict | None = None,
                     use_synonyms: bool = True) -> Server:
    cfg = config or load_datasheets_config()  # 配置

    def version_of(mpn: str, version: str | None) -> str:  # 解析版本
        if version is not None:  # 指定了
            if not any(v["version"] == version for v in store.versions(mpn)):  # 不存在
                raise NotFound(f"{mpn} 没有版本 {version[:12]}")  # 查不到
            return version  # 用它
        meta = latest(store, mpn)  # 最新
        if meta is None:  # 还没入库
            raise NotFound(f"{mpn} 的 datasheet 还没取得，先用 find_datasheet")  # 引导
        return meta["version"]  # 最新版本

    def find(a: FindArgs, ctx):  # 找 datasheet
        try:  # 按需入库
            meta = ingestor.ensure(a.mpn, day=ctx.day)  # 首次才获取
        except FetchRejected as exc:  # 获取失败
            raise NotFound(f"取不到 {a.mpn} 的 datasheet：{exc}") from None  # 查不到
        return {"mpn": a.mpn, "version": meta["version"], "title": meta["title"],  # 版本与标题
                "pages": len(store.pages(a.mpn, meta["version"])), "toc_entries": len(meta["toc"])}  # 页数与目录

    def toc(a: TocArgs, ctx):  # 目录
        v = version_of(a.mpn, a.version)  # 版本
        meta = next(m for m in store.versions(a.mpn) if m["version"] == v)  # 元数据
        return [{"title": t["title"], "page": t["page"]} for t in meta["toc"]]  # 目录

    def find_pages(a: SearchArgs, ctx):  # 检索
        v = version_of(a.mpn, a.version)  # 版本
        hits = search(store, a.mpn, v, a.query, synonyms=cfg["synonyms"], use_synonyms=use_synonyms,  # 检索
                      limit=cfg["search"]["limit"], snippet_chars=cfg["search"]["snippet_chars"])  # 限制
        return [{"page": h["page"], "snippet": h["snippet"], "version": v} for h in hits]  # 页码与片段

    def read(a: PageArgs, ctx):  # 读页
        v = version_of(a.mpn, a.version)  # 版本
        body = store.page(a.mpn, v, a.page)  # 页文本
        if body is None:  # 没有这页
            raise NotFound(f"{a.mpn} 版本 {v[:12]} 没有第 {a.page} 页")  # 查不到
        return {"mpn": a.mpn, "version": v, "page": a.page, "text": body}  # 原文

    def keys(a):  # 实体键
        return {"mpn": [a.mpn], "document": [f"datasheet:{a.mpn}"]}  # 料号与文档

    return Server("datasheet", [
        ToolDef("find_datasheet", "找某颗料的 datasheet（首次会下载解析），返回版本、页数、标题。何时用：要查参数原文之前。"
                "例：find_datasheet(mpn='SKY13453-385LF')", FindArgs, find, source="external",
                providers=lambda a: ["datasheet"], entity_keys=keys, scoped=True),  # 按需入库
        ToolDef("datasheet_toc", "看 datasheet 目录（章节 → 页码）。例：datasheet_toc(mpn='SKY13453-385LF')",
                TocArgs, toc, entity_keys=keys, scoped=True),  # 目录
        ToolDef("datasheet_search", "在 datasheet 里按参数名或关键词找页码与片段（支持中英同义词）。"
                "例：datasheet_search(mpn='SKY13453-385LF', query='isolation')", SearchArgs, find_pages,
                entity_keys=keys, scoped=True),  # 检索
        ToolDef("datasheet_read_page", "读 datasheet 某一页原文；引用时写 料号 + 版本 + 页码。何时别用：没检索就逐页翻。"
                "例：datasheet_read_page(mpn='SKY13453-385LF', page=2)", PageArgs, read, entity_keys=keys,
                scoped=True),  # 读页
    ])
