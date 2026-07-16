"""PDF 抽取器单元测试。"""
from __future__ import annotations

import pytest

from app.core.ingest.errors import (
    CORRUPT_FILE,
    PDF_ENCRYPTED,
    PDF_NO_TEXT_LAYER,
    IngestError,
)
from app.core.ingest.extractors.pdf import PdfExtractor
from pdf_fixtures import make_pdf_bytes


def test_pdf_extractor_multipage() -> None:
    data = make_pdf_bytes(["Hello page one", "Hello page two"])
    doc = PdfExtractor().extract(data, original_filename="report.pdf")
    assert "--- Page 1 ---" in doc.text
    assert "--- Page 2 ---" in doc.text
    assert doc.source_uri.startswith("upload://")
    assert doc.metadata["page_count"] == 2


def test_pdf_extractor_corrupt() -> None:
    with pytest.raises(IngestError) as exc:
        PdfExtractor().extract(b"not a pdf", original_filename="x.pdf")
    assert exc.value.code == CORRUPT_FILE


def test_pdf_extractor_no_text_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    import fitz

    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    with pytest.raises(IngestError) as exc:
        PdfExtractor().extract(data, original_filename="blank.pdf")
    assert exc.value.code == PDF_NO_TEXT_LAYER


def test_pdf_extractor_encrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDoc:
        needs_pass = True
        page_count = 1

        def load_page(self, i):
            return None

        def close(self):
            pass

    monkeypatch.setattr(
        "app.core.ingest.extractors.pdf.fitz.open",
        lambda **kwargs: FakeDoc(),
    )
    with pytest.raises(IngestError) as exc:
        PdfExtractor().extract(b"%PDF-1.4 fake", original_filename="enc.pdf")
    assert exc.value.code == PDF_ENCRYPTED
