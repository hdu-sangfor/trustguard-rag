"""Multi-format extractor and MIME routing tests."""
from __future__ import annotations

import json

import pytest

from app.core.ingest.errors import EMPTY_CONTENT, OCR_UNAVAILABLE, UNSUPPORTED_MIME, IngestError
from app.core.ingest.extractors.file import FileExtractor, SUPPORTED_MIME_TYPES
from app.core.ingest.extractors.html_extractor import HtmlExtractor
from app.core.ingest.extractors.image_extractor import ImageExtractor
from app.core.ingest.extractors.text_extractor import (
    CsvExtractor,
    JsonExtractor,
    MarkdownExtractor,
    PlainTextExtractor,
)
from app.core.ocr.protocol import OcrRecognizeResult
from app.settings import get_settings


class FakeOcr:
    name = "fake"

    def __init__(self, text: str = "图中文字", empty: bool = False) -> None:
        self._text = text
        self._empty = empty
        self.enabled = True
        self.provider_name = "fake"

    async def recognize(self, image_bytes, *, lang=None, fail_open=None):
        return OcrRecognizeResult(
            text="" if self._empty else self._text,
            confidence=0.8,
            empty=self._empty,
        )

    async def recognize_region(self, *, page_no, bbox, crop_png, lang=None):
        from app.core.ocr.protocol import OcrRegionDraft

        text = "" if self._empty else self._text
        return OcrRegionDraft(
            page_no=page_no,
            bbox=bbox,
            crop_png=crop_png,
            ocr_text=text,
            status="empty" if self._empty else "pending",
            provider="fake",
            confidence=0.8,
        )


def test_supported_mime_includes_formats():
    assert "application/pdf" in SUPPORTED_MIME_TYPES
    assert "text/plain" in SUPPORTED_MIME_TYPES
    assert "text/markdown" in SUPPORTED_MIME_TYPES
    assert "image/png" in SUPPORTED_MIME_TYPES


def test_plain_text_and_gbk():
    data = "你好世界".encode("gbk")
    doc = PlainTextExtractor().extract(data, original_filename="a.txt")
    assert "你好世界" in doc.text


def test_markdown_front_matter():
    raw = b"---\ntitle: t\n---\n# Hello\nbody"
    doc = MarkdownExtractor().extract(raw, original_filename="a.md")
    assert "Hello" in doc.text
    assert "front_matter" in doc.metadata


def test_csv_json_html():
    csv_doc = CsvExtractor().extract(b"a,b\n1,2\n", original_filename="a.csv")
    assert "a | b" in csv_doc.text
    json_doc = JsonExtractor().extract(json.dumps({"k": "v"}).encode(), original_filename="a.json")
    assert '"k"' in json_doc.text
    html_doc = HtmlExtractor().extract(
        b"<html><script>x</script><body><p>Hi</p></body></html>",
        original_filename="a.html",
    )
    assert "Hi" in html_doc.text
    assert "x" not in html_doc.text


def test_empty_text_fails():
    with pytest.raises(IngestError) as ei:
        PlainTextExtractor().extract(b"   ", original_filename="a.txt")
    assert ei.value.code == EMPTY_CONTENT


def test_file_router_magic_pdf_and_unsupported():
    fe = FileExtractor()
    with pytest.raises(IngestError) as ei:
        fe.extract(b"not-a-known", original_filename="x.bin", mime="application/octet-stream")
    assert ei.value.code == UNSUPPORTED_MIME


@pytest.mark.asyncio
async def test_image_extractor_requires_ocr(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "none")
    get_settings.cache_clear()
    with pytest.raises(IngestError) as ei:
        await ImageExtractor().extract_async(
            b"\x89PNG\r\n\x1a\n", original_filename="a.png", mime="image/png"
        )
    assert ei.value.code == OCR_UNAVAILABLE


@pytest.mark.asyncio
async def test_image_extractor_with_fake_ocr():
    doc = await ImageExtractor(ocr_engine=FakeOcr("扫描文字")).extract_async(
        b"\x89PNG\r\n\x1a\nfake",
        original_filename="a.png",
        mime="image/png",
    )
    assert doc.text == "扫描文字"
    assert doc.metadata["ocr_region_drafts"]


@pytest.mark.asyncio
async def test_image_empty_ocr_still_returns_drafts():
    doc = await ImageExtractor(ocr_engine=FakeOcr(empty=True)).extract_async(
        b"\x89PNG\r\n\x1a\nfake",
        original_filename="a.png",
        mime="image/png",
    )
    assert doc.text == ""
    assert doc.metadata["ocr_region_drafts"][0].status == "empty"


@pytest.mark.asyncio
async def test_sources_capabilities(client):
    resp = await client.get("/v1/sources/capabilities")
    assert resp.status_code == 200
    mimes = resp.json()["sources"][0]["mime_types"]
    assert "text/markdown" in mimes
    assert "image/png" in mimes


def test_file_router_txt_md(tmp_path):
    fe = FileExtractor()
    txt = fe.extract(b"hello txt", original_filename="note.txt")
    assert txt.mime == "text/plain"
    md = fe.extract(b"# title\n", original_filename="note.md")
    assert md.mime == "text/markdown"
