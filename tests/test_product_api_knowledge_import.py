import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from product_api.knowledge_import import KnowledgeParseError, parse_knowledge_file


def _text_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        }
    )
    stream = StreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_markdown_keeps_stable_section_locators() -> None:
    parsed = parse_knowledge_file(
        "policy.md",
        b"# Discount Approval\n\nManager review is required.\n\n# Delivery\n\nDo not promise estimates.",
    )

    assert [chunk.locator for chunk in parsed.chunks] == [
        "section:discount-approval",
        "section:delivery",
    ]
    assert "Manager review" in parsed.normalized_content


def test_text_is_utf8_and_has_one_document_chunk() -> None:
    parsed = parse_knowledge_file("sales.txt", "授权渠道需要追溯记录。".encode())
    assert len(parsed.chunks) == 1
    assert parsed.chunks[0].locator == "document"


def test_pdf_extracts_page_locator_without_network_or_ocr() -> None:
    parsed = parse_knowledge_file("max2837.pdf", _text_pdf("MAX2837 official page"))
    assert parsed.chunks[0].locator == "page:1"
    assert "MAX2837" in parsed.chunks[0].body


def test_empty_encrypted_or_unsupported_input_fails_closed() -> None:
    with pytest.raises(KnowledgeParseError, match="1 byte"):
        parse_knowledge_file("empty.txt", b"")
    with pytest.raises(KnowledgeParseError, match="UTF-8"):
        parse_knowledge_file("bad.txt", b"\xff")
    with pytest.raises(KnowledgeParseError, match="只支持"):
        parse_knowledge_file("sheet.xlsx", b"not empty")

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(KnowledgeParseError, match="加密 PDF"):
        parse_knowledge_file("secret.pdf", output.getvalue())


def test_pdf_without_extractable_text_requires_manual_conversion() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(KnowledgeParseError, match="不提供 OCR"):
        parse_knowledge_file("scan.pdf", output.getvalue())


def test_demo_corpus_is_parseable_and_source_kinds_are_explicit() -> None:
    root = Path(__file__).parents[1] / "data" / "knowledge" / "demo"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["dataset"] == "supplyagent-f36-demo-v1"
    assert {document["source_kind"] for document in manifest["documents"]} == {
        "official",
        "synthetic",
    }
    for document in manifest["documents"]:
        path = root / document["file"]
        parsed = parse_knowledge_file(path.name, path.read_bytes())
        assert parsed.chunks
        if document["source_kind"] == "official":
            assert document["source_urls"]
        else:
            assert "SYNTHETIC DEMO DATA" in parsed.normalized_content
