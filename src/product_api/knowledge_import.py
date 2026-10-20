"""F36 本地知识解析；无网络、无 OCR、无远程解析服务。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath

from pypdf import PdfReader

from product_api.knowledge import KnowledgeChunkInput

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_NORMALIZED_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_CHUNKS = 1_000
MAX_CHUNK_CHARS = 20_000


class KnowledgeParseError(ValueError):
    """文件类型、大小、加密状态或正文不满足受控导入规则。"""


@dataclass(frozen=True)
class ParsedKnowledgeDocument:
    normalized_content: str
    chunks: tuple[KnowledgeChunkInput, ...]


def parse_knowledge_file(filename: str, content: bytes) -> ParsedKnowledgeDocument:
    """按扩展名解析受控字节；调用者负责元数据、权限和数据库事务。"""
    if not filename or len(filename) > 240:
        raise KnowledgeParseError("文件名必须是 1–240 字符")
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise KnowledgeParseError("源文件必须是 1 byte–20 MiB")
    suffix = PurePath(filename).suffix.lower()
    if suffix in {".md", ".txt"}:
        return _parse_text(content, markdown=suffix == ".md")
    if suffix == ".pdf":
        return _parse_pdf(content)
    raise KnowledgeParseError("第一版只支持 Markdown、TXT 和 PDF")


def _parse_text(content: bytes, *, markdown: bool) -> ParsedKnowledgeDocument:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise KnowledgeParseError("Markdown/TXT 必须使用 UTF-8") from error
    normalized = _normalize(text)
    sections = _markdown_sections(normalized) if markdown else [("document", normalized)]
    return _finish(normalized, sections)


def _parse_pdf(content: bytes) -> ParsedKnowledgeDocument:
    try:
        reader = PdfReader(BytesIO(content), strict=True)
    except Exception as error:
        raise KnowledgeParseError("PDF 结构无效") from error
    if reader.is_encrypted:
        raise KnowledgeParseError("第一版不导入加密 PDF")
    if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
        raise KnowledgeParseError("PDF 必须包含 1–500 页")
    sections: list[tuple[str, str]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        if "/Contents" not in page:
            continue
        try:
            page_text = _normalize(page.extract_text() or "")
        except Exception as error:
            raise KnowledgeParseError(f"PDF 第 {page_number} 页无法安全提取") from error
        if page_text:
            sections.append((f"page:{page_number}", page_text))
    if not sections:
        raise KnowledgeParseError("PDF 未提取到正文；第一版不提供 OCR")
    normalized = "\n\n".join(text for _, text in sections)
    return _finish(normalized, sections)


def _normalize(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip()
    if not normalized:
        raise KnowledgeParseError("文档正文不能为空")
    if len(normalized.encode("utf-8")) > MAX_NORMALIZED_BYTES:
        raise KnowledgeParseError("抽取后的正文不能超过 5 MiB")
    return normalized


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    heading = "document"
    body: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            if any(part.strip() for part in body):
                sections.append((f"section:{_slug(heading)}", "\n".join(body).strip()))
            heading = match.group(1)
            body = [line]
        else:
            body.append(line)
    if any(part.strip() for part in body):
        sections.append((f"section:{_slug(heading)}", "\n".join(body).strip()))
    return sections


def _slug(value: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", value.lower()).strip("-")
    return slug[:160] or "document"


def _finish(
    normalized: str, sections: list[tuple[str, str]]
) -> ParsedKnowledgeDocument:
    chunks: list[KnowledgeChunkInput] = []
    used_locators: dict[str, int] = {}
    for base_locator, text in sections:
        if not text:
            continue
        parts = [text[index : index + MAX_CHUNK_CHARS] for index in range(0, len(text), MAX_CHUNK_CHARS)]
        for part_number, part in enumerate(parts, start=1):
            locator = base_locator if len(parts) == 1 else f"{base_locator}#part:{part_number}"
            duplicate_number = used_locators.get(locator, 0)
            used_locators[locator] = duplicate_number + 1
            if duplicate_number:
                locator = f"{locator}#duplicate:{duplicate_number + 1}"
            chunks.append(KnowledgeChunkInput(len(chunks), locator, part))
    if not chunks or len(chunks) > MAX_CHUNKS:
        raise KnowledgeParseError("解析结果必须包含 1–1000 个 chunk")
    return ParsedKnowledgeDocument(normalized, tuple(chunks))
