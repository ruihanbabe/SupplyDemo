"""文件摘要（F19 · datasheet 解析）

spec §6：版面感知解析（Docling），逐页得到文本与表格（Markdown），并抽出目录；以页为单位存储。
解析器接口：parse(data: bytes, content_type) → {title, toc: [{title, page}], pages: [{page, text}]}。
- FixtureParser：读固定样例（tests/fixtures/datasheets/*.json，合成内容），校验页码从 1 连续、目录页码存在。
- DoclingParser：惰性导入 Docling；未安装时抛 ParserUnavailable（"尚未具备：需安装 docling"），不静默成功。
  真实解析等用户同意新依赖与真实下载后启用（本 Feature 不测它的解析结果）。
依赖：标准库 json；docling（可选）。被 datasheets.fetch.Ingestor 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import json  # 解析样例

FIXTURE_TYPE = "application/x-datasheet-fixture+json"  # 样例的内容类型


class ParserUnavailable(RuntimeError):
    """解析器尚未具备（依赖未安装）。"""


class BadDocument(ValueError):
    """文档结构不对（页码不连续、目录指向不存在的页等）。"""


def check_document(doc: dict) -> dict:
    """结构校验：页码从 1 连续；目录页码都存在。"""
    pages = [p["page"] for p in doc["pages"]]  # 页码
    if pages != list(range(1, len(pages) + 1)):  # 不连续
        raise BadDocument(f"页码须从 1 连续：{pages}")  # 报错
    bad = [t for t in doc.get("toc", []) if t["page"] not in pages]  # 指向不存在的页
    if bad:  # 有
        raise BadDocument(f"目录指向不存在的页：{bad}")  # 报错
    return {"title": doc.get("title"), "toc": doc.get("toc", []), "pages": doc["pages"]}  # 规范结构


class FixtureParser:
    """固定样例解析器。"""

    def parse(self, data: bytes, content_type: str) -> dict:
        if content_type != FIXTURE_TYPE:  # 不是样例
            raise BadDocument(f"FixtureParser 只解析样例，收到 {content_type}")  # 报错
        return check_document(json.loads(data.decode("utf-8")))  # 解析并校验


class DoclingParser:
    """Docling 版面感知解析（需安装 docling，新依赖）。"""

    def parse(self, data: bytes, content_type: str) -> dict:
        try:  # 惰性导入
            import docling  # noqa: F401  # 只检查是否安装
        except ImportError:  # 未安装
            raise ParserUnavailable("尚未具备：需安装 docling（新依赖，需用户同意）") from None  # 明确报错
        raise ParserUnavailable("尚未具备：Docling 解析将在用户授权真实下载后接入")  # 已安装也不假装能用
