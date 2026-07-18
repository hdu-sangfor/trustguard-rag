"""MinerU API-backed extractors for Office documents."""

from __future__ import annotations

import hashlib
from pathlib import PurePath
from typing import Any

import httpx

from app.core.ingest.errors import (
    EMPTY_CONTENT,
    FILE_TOO_LARGE,
    MINERU_PARSE_FAILED,
    MINERU_UNAVAILABLE,
    IngestError,
)
from app.core.ingest.models import ExtractedDocument
from app.settings import Settings, get_settings

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _find_markdown(value: Any) -> str | None:
    """Find MinerU's Markdown field across compatible response envelopes."""
    if isinstance(value, dict):
        markdown = value.get("md_content")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
        for child in value.values():
            found = _find_markdown(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_markdown(child)
            if found:
                return found
    return None


class MineruDocumentExtractor:
    """Send a supported document to MinerU and return its Markdown."""

    def __init__(
        self,
        *,
        mime_type: str,
        raw_filename: str,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._mime_type = mime_type
        self._raw_filename = raw_filename
        self._settings = settings
        self._transport = transport

    async def extract_async(
        self,
        data: bytes,
        *,
        original_filename: str,
    ) -> ExtractedDocument:
        settings = self._settings or get_settings()
        if not data:
            raise IngestError(EMPTY_CONTENT, "Document is empty")
        max_bytes = (
            settings.ingest_max_pdf_bytes
            if self._mime_type == PDF_MIME
            else settings.ingest_max_file_bytes
        )
        if len(data) > max_bytes:
            raise IngestError(FILE_TOO_LARGE, "Document exceeds max byte size")

        url = f"{settings.mineru_base_url.rstrip('/')}/file_parse"
        form = {
            "backend": settings.mineru_backend,
            "return_md": "true",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_content_list": "false",
            "return_images": "false",
            "response_format_zip": "false",
        }
        expected_suffix = PurePath(self._raw_filename).suffix
        original_path = PurePath(original_filename)
        mineru_filename = original_filename
        if original_path.suffix.lower() != expected_suffix.lower():
            mineru_filename = f"{original_path.stem or 'document'}{expected_suffix}"
        files = {"files": (mineru_filename, data, self._mime_type)}
        try:
            async with httpx.AsyncClient(
                timeout=settings.mineru_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(url, data=form, files=files)
                response.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise IngestError(
                MINERU_UNAVAILABLE,
                f"MinerU service is unavailable at {settings.mineru_base_url}",
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise IngestError(
                MINERU_PARSE_FAILED,
                f"MinerU returned HTTP {exc.response.status_code}: {detail}",
            ) from exc
        except httpx.HTTPError as exc:
            raise IngestError(MINERU_UNAVAILABLE, f"MinerU request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise IngestError(MINERU_PARSE_FAILED, "MinerU returned invalid JSON") from exc
        markdown = _find_markdown(payload)
        if not markdown or not markdown.strip():
            raise IngestError(MINERU_PARSE_FAILED, "MinerU response contains no Markdown")

        text = markdown.strip()
        content_hash = hashlib.sha256(data).hexdigest()
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime=self._mime_type,
            raw_bytes=data,
            raw_filename=self._raw_filename,
            metadata={
                "original_filename": original_filename,
                "mineru_filename": mineru_filename,
                "file_size": len(data),
                "parser": "mineru",
                "mineru_backend": settings.mineru_backend,
                "extracted_format": "markdown",
            },
        )


class MineruPdfExtractor(MineruDocumentExtractor):
    """MinerU-backed PDF extractor."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            mime_type=PDF_MIME,
            raw_filename="raw.pdf",
            settings=settings,
            transport=transport,
        )


class MineruDocxExtractor(MineruDocumentExtractor):
    """MinerU-backed DOCX extractor."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            mime_type=DOCX_MIME,
            raw_filename="raw.docx",
            settings=settings,
            transport=transport,
        )
