"""PDF 图片区域 OCR 抽取测试。"""
from __future__ import annotations

import fitz
import pytest

from app.core.ingest.errors import PDF_NO_TEXT_LAYER, IngestError
from app.core.ingest.extractors.pdf import PdfExtractor
from app.core.ocr.protocol import OcrRecognizeResult, OcrRegionDraft
from app.settings import get_settings


class FakeOcrEngine:
    name = "fake"
    enabled = True
    provider_name = "fake"

    def __init__(self, text: str = "OCR_HIT", fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    async def recognize(self, image_bytes, *, lang=None, fail_open=None):
        return OcrRecognizeResult(text=self.text, confidence=0.7, empty=not self.text)

    async def recognize_region(self, *, page_no, bbox, crop_png, lang=None):
        if self.fail:
            return OcrRegionDraft(
                page_no=page_no,
                bbox=bbox,
                crop_png=crop_png,
                ocr_text="",
                status="failed",
                provider="fake",
                error_message="forced",
            )
        return OcrRegionDraft(
            page_no=page_no,
            bbox=bbox,
            crop_png=crop_png,
            ocr_text=self.text,
            status="pending" if self.text else "empty",
            provider="fake",
            confidence=0.7,
        )


def _pdf_with_embedded_image() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    # 构造小 RGB pixmap 并插入为图片（无 alpha）
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 80, 40), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    page.insert_image(fitz.Rect(50, 50, 200, 120), pixmap=pix)
    page.insert_text((72, 200), "visible text layer")
    data = doc.tobytes()
    doc.close()
    return data


def _scanned_like_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 60), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.asyncio
async def test_pdf_ocr_merges_image_text(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "api")
    get_settings.cache_clear()
    extractor = PdfExtractor(ocr_engine=FakeOcrEngine("from-image"))
    doc = await extractor.extract_async(_pdf_with_embedded_image(), original_filename="mix.pdf")
    assert "visible text layer" in doc.text
    assert "from-image" in doc.text
    assert doc.metadata["ocr_region_drafts"]
    assert 1 in doc.metadata["pages_with_ocr"]


@pytest.mark.asyncio
async def test_pdf_ocr_fail_open_region(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "api")
    monkeypatch.setenv("RAG_OCR_FAIL_OPEN", "true")
    get_settings.cache_clear()
    extractor = PdfExtractor(ocr_engine=FakeOcrEngine(fail=True))
    doc = await extractor.extract_async(_pdf_with_embedded_image(), original_filename="mix.pdf")
    assert doc.metadata["ocr_region_drafts"][0].status == "failed"
    assert "visible text layer" in doc.text


@pytest.mark.asyncio
async def test_pdf_no_text_without_ocr(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "none")
    get_settings.cache_clear()
    extractor = PdfExtractor()
    with pytest.raises(IngestError) as ei:
        await extractor.extract_async(_scanned_like_pdf(), original_filename="scan.pdf")
    assert ei.value.code == PDF_NO_TEXT_LAYER


@pytest.mark.asyncio
async def test_pdf_ocr_all_empty_returns_empty_text(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "api")
    get_settings.cache_clear()
    extractor = PdfExtractor(ocr_engine=FakeOcrEngine(text=""))
    doc = await extractor.extract_async(_scanned_like_pdf(), original_filename="scan.pdf")
    assert doc.text == ""
    assert doc.metadata["ocr_region_drafts"]
    assert doc.metadata["ocr_region_drafts"][0].status == "empty"
