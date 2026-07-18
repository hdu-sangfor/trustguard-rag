"""Text and Markdown extractor unit tests."""

from __future__ import annotations

import pytest

from app.core.ingest.errors import EMPTY_CONTENT, UNSUPPORTED_ENCODING, IngestError
from app.core.ingest.extractors.file import FileExtractor
from app.core.ingest.extractors.text import TextExtractor


def _text_extractor() -> TextExtractor:
    return TextExtractor(mime_type="text/plain", raw_filename="raw.txt")


def test_text_extractor_utf8() -> None:
    doc = _text_extractor().extract(
        "这是安全知识文档。".encode(),
        original_filename="security.txt",
    )

    assert doc.text == "这是安全知识文档。"
    assert doc.mime == "text/plain"
    assert doc.raw_filename == "raw.txt"
    assert doc.metadata["encoding"] == "utf-8"
    assert doc.metadata["parser"] == "utf8-passthrough"
    assert doc.metadata["original_filename"] == "security.txt"
    assert doc.source_uri.startswith("upload://")


def test_text_extractor_utf8_with_bom() -> None:
    data = "带 BOM 的文本".encode("utf-8-sig")
    doc = _text_extractor().extract(data, original_filename="bom.txt")

    assert doc.text == "带 BOM 的文本"


def test_text_extractor_empty() -> None:
    with pytest.raises(IngestError) as exc_info:
        _text_extractor().extract(b"   \n\n", original_filename="empty.txt")

    assert exc_info.value.code == EMPTY_CONTENT


def test_text_extractor_unsupported_encoding() -> None:
    data = "这是 GBK 文本".encode("gbk")

    with pytest.raises(IngestError) as exc_info:
        _text_extractor().extract(data, original_filename="gbk.txt")

    assert exc_info.value.code == UNSUPPORTED_ENCODING


def test_text_extractor_hash_is_stable() -> None:
    data = b"same content"
    first = _text_extractor().extract(data, original_filename="one.txt")
    second = _text_extractor().extract(data, original_filename="two.txt")

    assert first.content_hash == second.content_hash
    assert first.source_uri == second.source_uri


def test_file_extractor_routes_txt_by_filename() -> None:
    doc = FileExtractor().extract(
        "普通文本".encode(),
        original_filename="document.txt",
        mime=None,
    )

    assert doc.text == "普通文本"
    assert doc.mime == "text/plain"


def test_file_extractor_routes_markdown_by_filename() -> None:
    doc = FileExtractor().extract(
        "# 安全指南\n\n这是正文。".encode(),
        original_filename="guide.md",
        mime=None,
    )

    assert doc.text.startswith("# 安全指南")
    assert doc.mime == "text/markdown"
    assert doc.raw_filename == "raw.md"


def test_file_extractor_falls_back_from_generic_mime() -> None:
    doc = FileExtractor().extract(
        "# 文档".encode(),
        original_filename="guide.md",
        mime="application/octet-stream",
    )

    assert doc.mime == "text/markdown"
