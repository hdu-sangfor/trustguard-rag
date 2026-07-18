"""MinerU API-backed extractors for Office documents."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePath
from typing import Any

import fitz
import httpx

from app.core.ingest.errors import (
    EMPTY_CONTENT,
    FILE_TOO_LARGE,
    MINERU_PARSE_FAILED,
    MINERU_UNAVAILABLE,
    CORRUPT_FILE,
    PDF_ENCRYPTED,
    PDF_TOO_LARGE,
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


def _find_content_list(value: Any) -> list[dict[str, Any]] | None:
    """Find and decode MinerU's structured content list across response envelopes."""
    if isinstance(value, dict):
        candidate = value.get("content_list")
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                candidate = None
        if isinstance(candidate, list) and all(isinstance(item, dict) for item in candidate):
            return candidate
        for child in value.values():
            found = _find_content_list(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_content_list(child)
            if found is not None:
                return found
    return None


def _content_item_text(item: dict[str, Any]) -> str:
    """Render the stable textual fields exposed by MinerU content-list items."""
    parts: list[str] = []
    for key in ("text", "table_body", "latex"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for key in ("img_caption", "img_footnote", "table_caption", "table_footnote"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            parts.extend(str(part).strip() for part in value if str(part).strip())
    return "\n\n".join(dict.fromkeys(parts))


def _page_aware_text(items: list[dict[str, Any]]) -> str:
    pages: dict[int, list[str]] = {}
    for item in items:
        page_idx = item.get("page_idx")
        if not isinstance(page_idx, int) or page_idx < 0:
            continue
        text = _content_item_text(item)
        if text:
            pages.setdefault(page_idx + 1, []).append(text)
    return "\n\n".join(
        f"--- Page {page_no} ---\n" + "\n\n".join(parts)
        for page_no, parts in sorted(pages.items())
        if parts
    ).strip()


def _validate_pdf(data: bytes, *, max_pages: int) -> int:
    """Reject malformed, encrypted, or oversized PDFs before sending them to MinerU."""
    if not data.startswith(b"%PDF-"):
        raise IngestError(CORRUPT_FILE, "Not a valid PDF file")
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise IngestError(CORRUPT_FILE, f"Cannot open PDF: {exc}") from exc
    try:
        if document.needs_pass:
            raise IngestError(PDF_ENCRYPTED, "Password-protected PDF not supported")
        page_count = document.page_count
        if page_count > max_pages:
            raise IngestError(PDF_TOO_LARGE, "PDF exceeds max page count")
        return page_count
    finally:
        document.close()


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
        page_count = (
            _validate_pdf(data, max_pages=settings.ingest_max_pdf_pages)
            if self._mime_type == PDF_MIME
            else None
        )

        url = f"{settings.mineru_base_url.rstrip('/')}/file_parse"
        form = {
            "backend": settings.mineru_backend,
            "return_md": "true",
            "return_middle_json": "false",
            "return_model_output": "false",
            "return_content_list": "true",
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
            code = exc.response.status_code
            if code == 429 or code >= 500:
                raise IngestError(
                    MINERU_UNAVAILABLE,
                    f"MinerU is temporarily unavailable (HTTP {code}): {detail}",
                ) from exc
            raise IngestError(
                MINERU_PARSE_FAILED,
                f"MinerU returned HTTP {code}: {detail}",
            ) from exc
        except httpx.HTTPError as exc:
            raise IngestError(MINERU_UNAVAILABLE, f"MinerU request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise IngestError(MINERU_PARSE_FAILED, "MinerU returned invalid JSON") from exc
        content_list = _find_content_list(payload)
        page_text = _page_aware_text(content_list or [])
        markdown = _find_markdown(payload)
        text = page_text or (markdown or "").strip()
        if not text:
            raise IngestError(MINERU_PARSE_FAILED, "MinerU response contains no Markdown")

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
                "page_count": page_count,
                "page_metadata_preserved": bool(page_text),
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
