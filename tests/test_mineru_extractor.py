"""MinerU document extractor unit tests."""

from __future__ import annotations

import httpx
import pytest

from app.core.ingest.extractors import file as file_module
from app.core.ingest.errors import MINERU_PARSE_FAILED, MINERU_UNAVAILABLE, IngestError
from app.core.ingest.extractors.file import MIME_ROUTER, FileExtractor
from app.core.ingest.extractors.mineru import (
    DOCX_MIME,
    PDF_MIME,
    MineruDocxExtractor,
    MineruPdfExtractor,
)
from app.settings import Settings, get_settings


def _settings() -> Settings:
    return Settings(
        mineru_base_url="http://mineru.test:8000",
        mineru_backend="pipeline",
        mineru_timeout_seconds=5,
    )


def _success_transport(
    markdown: str = "# Word 标题\n\n由 MinerU 提取的正文。",
    *,
    expected_filename: str = "report.docx",
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://mineru.test:8000/file_parse"
        assert request.method == "POST"
        body = request.read()
        assert expected_filename.encode() in body
        assert b'name="return_md"' in body
        return httpx.Response(
            200,
            json={"results": {"report": {"md_content": markdown}}},
        )

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_mineru_docx_extractor_returns_markdown() -> None:
    extractor = MineruDocxExtractor(_settings(), transport=_success_transport())

    doc = await extractor.extract_async(b"fake docx bytes", original_filename="report.docx")

    assert doc.text.startswith("# Word 标题")
    assert doc.mime == DOCX_MIME
    assert doc.raw_filename == "raw.docx"
    assert doc.metadata["parser"] == "mineru"
    assert doc.metadata["mineru_backend"] == "pipeline"
    assert doc.metadata["extracted_format"] == "markdown"


@pytest.mark.asyncio
async def test_file_extractor_routes_docx_to_mineru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mineru = MineruDocxExtractor(_settings(), transport=_success_transport("DOCX content"))
    monkeypatch.setitem(MIME_ROUTER, DOCX_MIME, mineru)

    doc = await FileExtractor().extract_async(
        b"fake docx bytes",
        original_filename="report.docx",
        mime="application/octet-stream",
    )

    assert doc.text == "DOCX content"
    assert doc.mime == DOCX_MIME


@pytest.mark.asyncio
async def test_file_extractor_routes_pdf_signature_to_mineru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mineru = MineruPdfExtractor(
        _settings(),
        transport=_success_transport("PDF content", expected_filename="incorrect.pdf"),
    )
    monkeypatch.setenv("RAG_PDF_PARSER", "mineru")
    get_settings.cache_clear()
    monkeypatch.setattr(file_module, "_mineru_pdf", mineru)

    doc = await FileExtractor().extract_async(
        b"%PDF-1.4 fake bytes",
        original_filename="incorrect.txt",
        mime="text/plain",
    )

    assert doc.text == "PDF content"
    assert doc.mime == PDF_MIME
    assert doc.raw_filename == "raw.pdf"
    assert doc.metadata["parser"] == "mineru"
    assert doc.metadata["original_filename"] == "incorrect.txt"
    assert doc.metadata["mineru_filename"] == "incorrect.pdf"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_mineru_docx_extractor_rejects_missing_markdown() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"results": {}}))
    extractor = MineruDocxExtractor(_settings(), transport=transport)

    with pytest.raises(IngestError) as exc_info:
        await extractor.extract_async(b"fake docx bytes", original_filename="report.docx")

    assert exc_info.value.code == MINERU_PARSE_FAILED


@pytest.mark.asyncio
async def test_mineru_docx_extractor_reports_unavailable_service() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    extractor = MineruDocxExtractor(_settings(), transport=httpx.MockTransport(handler))

    with pytest.raises(IngestError) as exc_info:
        await extractor.extract_async(b"fake docx bytes", original_filename="report.docx")

    assert exc_info.value.code == MINERU_UNAVAILABLE
