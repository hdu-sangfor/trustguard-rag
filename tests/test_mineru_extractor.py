"""MinerU document extractor unit tests."""

from __future__ import annotations

import httpx
import fitz
import pytest

from app.core.ingest.extractors import file as file_module
from app.core.ingest.errors import (
    MINERU_PARSE_FAILED,
    MINERU_UNAVAILABLE,
    PDF_TOO_LARGE,
    IngestError,
)
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


def test_settings_default_pdf_parser_is_mineru(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_PDF_PARSER", raising=False)
    assert Settings(_env_file=None).pdf_parser == "mineru"


def _pdf_bytes() -> bytes:
    document = fitz.open()
    document.new_page().insert_text((72, 72), "PDF content")
    data = document.tobytes()
    document.close()
    return data


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
        assert b'name="return_content_list"' in body
        assert b"true" in body
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
        _pdf_bytes(),
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


@pytest.mark.asyncio
async def test_mineru_pdf_preserves_content_list_page_numbers() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "results": {
                    "report": {
                        "md_content": "fallback markdown",
                        "content_list": [
                            {"type": "text", "page_idx": 0, "text": "第一页"},
                            {"type": "table", "page_idx": 1, "table_body": "| A |"},
                        ],
                    }
                }
            },
        )
    )

    doc = await MineruPdfExtractor(_settings(), transport=transport).extract_async(
        _pdf_bytes(), original_filename="report.pdf"
    )

    assert "--- Page 1 ---\n第一页" in doc.text
    assert "--- Page 2 ---\n| A |" in doc.text
    assert doc.metadata["page_metadata_preserved"] is True


@pytest.mark.asyncio
async def test_mineru_pdf_enforces_page_limit_before_request() -> None:
    document = fitz.open()
    document.new_page()
    document.new_page()
    data = document.tobytes()
    document.close()
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    settings = _settings().model_copy(update={"ingest_max_pdf_pages": 1})
    extractor = MineruPdfExtractor(settings, transport=httpx.MockTransport(handler))
    with pytest.raises(IngestError) as exc_info:
        await extractor.extract_async(data, original_filename="report.pdf")

    assert exc_info.value.code == PDF_TOO_LARGE
    assert called is False


@pytest.mark.asyncio
async def test_mineru_server_error_is_retryable_unavailable() -> None:
    extractor = MineruDocxExtractor(
        _settings(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503, text="warming up")
        ),
    )

    with pytest.raises(IngestError) as exc_info:
        await extractor.extract_async(b"fake docx bytes", original_filename="report.docx")

    assert exc_info.value.code == MINERU_UNAVAILABLE
