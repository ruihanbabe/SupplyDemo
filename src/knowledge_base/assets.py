"""文件摘要（F20 · 知识资产）

spec §6 表：术语表（代码按关键词匹配注入，兼作检索同义词扩展）、比对清单与替代规则（节点强制注入）、Skills（SKILL.md）、
BOM 与板级文档（README 原文存档，与 datasheet 共用文档工具）。
- Glossary：读 config/knowledge/glossary/*.yaml。synonyms() 给 F19 检索做同义词组；inject(text, categories, limit) 返回文本里
  出现的术语说明（消融 glossary_injection 关闭时返回空）。
- category_of(mpn)：按 config/knowledge/categories.yaml 的正则前缀判品类（匹配不上为 None）；checklist(category) 读比对清单。
- load_skill(name)：skills/<名>/SKILL.md 正文与 SHA-256（运行清单用）；list_skills()。
- seed_rows()：读原始层原理图的 Substitution 字段（知识库 B 种子来源，先校验 MANIFEST）。
- 板级文档：BoardDocSource 读原始层 README（文档键 board:<板>），MarkdownParser 按一、二级标题切页；
  CompositeSource / CompositeParser 让 board: 前缀走板级文档、其余走 datasheet 来源，四个文档工具通用。
依赖：datasheets.parse（结构校验）、session.log.sha256、PyYAML。被 F19 Server（同义词、板级文档）、F22/F23（注入与 Skill）、F28 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 匹配
from pathlib import Path  # 路径

import yaml  # 读配置

from datasheets.parse import check_document  # 页结构校验
from session.log import sha256  # 哈希

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
KNOWLEDGE = ROOT / "config" / "knowledge"  # 知识配置目录
SKILLS = ROOT / "skills"  # Skills 目录
RAW = ROOT / "data" / "hardware" / "raw"  # 原理图与板级文档原始层
MARKDOWN = "text/markdown"  # 板级文档内容类型


def _word(term: str) -> re.Pattern:
    """英文词边界匹配（不跨进别的英文词；中文前后不设边界，因为中文词之间没有空格）。"""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term.lower())}(?![A-Za-z0-9_])")  # 正则


class Glossary:
    # 功能：见文件摘要。依赖：术语表目录、注入开关。完成：同一品类内术语重复即报错（维护错误早发现）。
    def __init__(self, directory: Path = KNOWLEDGE / "glossary", *, enabled: bool = True):
        self.enabled = enabled  # 消融开关 glossary_injection
        self.entries: list[dict] = []  # 全部术语（带品类）
        for path in sorted(Path(directory).glob("*.yaml")):  # 每个品类
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))  # 读
            seen = set()  # 本品类已有术语
            for t in spec["terms"]:  # 每条
                if not all(isinstance(x, str) for x in [t["term"], *t["synonyms"]]):  # YAML 把 0402 之类读成数字
                    raise ValueError(f"{path.name} 术语 {t['term']!r} 有非文本写法，数字样写法须加引号")  # 报错
                if t["term"].lower() in seen:  # 重复
                    raise ValueError(f"{path.name} 术语重复：{t['term']}")  # 报错
                seen.add(t["term"].lower())  # 记下
                self.entries.append({**t, "category": spec["category"]})  # 收下

    def categories(self) -> list[str]:
        """有术语表的品类。"""
        return sorted({e["category"] for e in self.entries})  # 去重排序

    def synonyms(self) -> list[list[str]]:
        """同义词组（给 F19 检索）：每条术语一组，跨品类同名术语合并。"""
        groups: dict[str, set[str]] = {}  # 术语 → 写法
        for e in self.entries:  # 每条
            groups.setdefault(e["term"].lower(), {e["term"]}).update(e["synonyms"])  # 合并
        return [sorted(g, key=str.lower) for _, g in sorted(groups.items())]  # 组列表

    def inject(self, text: str, categories: list[str] | None = None, limit: int = 8) -> list[str]:
        """文本里出现的术语说明（按出现先后，最多 limit 条）；关闭注入时为空。"""
        if not self.enabled:  # 消融
            return []  # 不注入
        low, found = text.lower(), []  # 小写文本与结果
        for e in self.entries:  # 每条
            if categories is not None and e["category"] not in categories:  # 品类不符
                continue  # 跳过
            hits = [m.start() for w in [e["term"], *e["synonyms"]] for m in _word(w).finditer(low)]  # 出现位置
            if hits:  # 出现了
                found.append((min(hits), f"术语：{e['term']}（{'、'.join(e['synonyms'])}）——{e['note']}"))  # 说明
        return [line for _, line in sorted(found)[:limit]]  # 按出现先后


def category_of(mpn: str, path: Path = KNOWLEDGE / "categories.yaml") -> str | None:
    """料号 → 品类（正则前缀，先匹配先用）。"""
    for c in yaml.safe_load(Path(path).read_text(encoding="utf-8"))["categories"]:  # 每条规则
        if re.match(c["pattern"], mpn):  # 命中
            return c["category"]  # 品类
    return None  # 未分类


def checklist(category: str) -> dict | None:
    """品类比对清单（config/checklists/<品类>.yaml），没有为 None。"""
    path = ROOT / "config" / "checklists" / f"{category}.yaml"  # 路径
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None  # 读


def list_skills() -> list[str]:
    """全部 Skill 名。"""
    return sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))  # 目录名


def load_skill(name: str) -> dict:
    """Skill 正文、描述与哈希；不存在报错。"""
    path = SKILLS / name / "SKILL.md"  # 路径
    body = path.read_text(encoding="utf-8")  # 正文
    head = re.match(r"^---\n(.*?)\n---\n", body, re.DOTALL)  # 头部元数据
    meta = yaml.safe_load(head.group(1)) if head else {}  # 解析
    return {"name": name, "description": meta.get("description", ""), "text": body, "sha256": sha256(body)}  # 结果


def seed_rows(raw: Path = RAW) -> list[dict]:
    """原理图里每个料号的 Substitution 字段（知识库 B 种子来源），先按 MANIFEST 校验原始层。"""
    import json  # 读清单

    from knowledge.fetch import load_registry, verify  # 登记与校验
    from knowledge.kicad import read_schematic  # 读原理图
    from knowledge.parts import build_parts  # 料号表

    rows = []  # 结果
    for project in load_registry():  # 每块板
        pid = project["project_id"]  # 板标识
        changed = verify(pid, raw)  # 哈希校验
        if changed:  # 被改过
            raise ValueError(f"{pid}: files differ from MANIFEST: {changed}")  # 拒绝使用
        manifest = json.loads((raw / pid / "MANIFEST.json").read_text(encoding="utf-8"))  # 清单
        for part in build_parts(read_schematic(raw / pid / manifest["root_schematic"])):  # 每个料号
            rows.append({"board": pid, "mpn": part.mpn, "alternates": part.alternates,  # 替代字段
                         "designators": list(part.designators)})  # 位号
    return rows  # 行


class BoardDocSource:
    """板级文档来源：文档键 board:<板> → 原始层该板的 README 原文（只读，不结构化）。"""

    def __init__(self, raw: Path = RAW):
        self.raw = Path(raw)  # 原始层

    def get(self, key: str) -> tuple[str, bytes, str]:
        board = key.removeprefix("board:")  # 板标识
        found = [p for p in (self.raw / board).glob("*") if p.name.lower() == "readme.md"]  # README（大小写不定）
        if not found:  # 没有
            raise LookupError(f"{board} 没有存档的 README")  # 报错
        return f"raw://{board}/{found[0].name}", found[0].read_bytes(), MARKDOWN  # 地址、原文、类型


class MarkdownParser:
    """按一、二级标题切页；标题前的内容为第 1 页；目录 = 各页标题。"""

    def parse(self, data: bytes, content_type: str) -> dict:
        text = data.decode("utf-8")  # 原文
        chunks = [c for c in re.split(r"(?m)^(?=#{1,2} )", text) if c.strip()]  # 按标题切
        pages = [{"page": i, "text": c.strip()} for i, c in enumerate(chunks, 1)]  # 页
        toc = [{"title": c.strip().splitlines()[0].lstrip("# ").strip(), "page": i}  # 目录
               for i, c in enumerate(chunks, 1) if c.lstrip().startswith("#")]  # 只收有标题的页
        return check_document({"title": toc[0]["title"] if toc else None, "toc": toc, "pages": pages})  # 校验


class CompositeSource:
    """按文档键前缀选来源：board: → 板级文档，其余 → 缺省来源（datasheet）。"""

    def __init__(self, default, board: BoardDocSource | None = None):
        self.default, self.board = default, board or BoardDocSource()  # 两个来源

    def get(self, key: str):
        return (self.board if key.startswith("board:") else self.default).get(key)  # 分派


class CompositeParser:
    """按内容类型选解析器：Markdown → MarkdownParser，其余 → 缺省解析器。"""

    def __init__(self, default):
        self.default, self.md = default, MarkdownParser()  # 两个解析器

    def parse(self, data: bytes, content_type: str) -> dict:
        return (self.md if content_type == MARKDOWN else self.default).parse(data, content_type)  # 分派
