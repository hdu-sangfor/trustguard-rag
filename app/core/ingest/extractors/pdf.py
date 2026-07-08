"""PDF text extraction via PyMuPDF."""
from __future__ import annotations

import hashlib
from typing import Any

import fitz

from app.core.ingest.errors import (
    CORRUPT_FILE,
    EMPTY_CONTENT,
    PDF_ENCRYPTED,
    PDF_NO_TEXT_LAYER,
    PDF_TOO_LARGE,
    IngestError,
)
from app.core.ingest.models import ExtractedDocument
from app.settings import get_settings

PDF_MAGIC = b"%PDF-"


class PdfExtractor:
    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.pdf",
    ) -> ExtractedDocument:
        settings = get_settings()
        if len(data) > settings.ingest_max_pdf_bytes:
            raise IngestError(PDF_TOO_LARGE, "PDF exceeds max byte size")
        if not data.startswith(PDF_MAGIC):
            raise IngestError(CORRUPT_FILE, "Not a valid PDF file")

        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            raise IngestError(CORRUPT_FILE, f"Cannot open PDF: {e}") from e

        try:
            if doc.needs_pass:
                raise IngestError(PDF_ENCRYPTED, "Password-protected PDF not supported")
            page_count = doc.page_count
            if page_count > settings.ingest_max_pdf_pages:
                raise IngestError(PDF_TOO_LARGE, "PDF exceeds max page count")

            pages_with_text: list[int] = []
            parts: list[str] = []
            for i in range(page_count):
                page_no = i + 1
                text = doc.load_page(i).get_text("text") or ""
                if text.strip():
                    pages_with_text.append(page_no)
                parts.append(f"\n--- Page {page_no} ---\n{text}")

            if not pages_with_text:
                raise IngestError(PDF_NO_TEXT_LAYER, "PDF has no extractable text layer")

            full_text = "".join(parts).strip()
            if not full_text:
                raise IngestError(EMPTY_CONTENT, "Extracted text is empty")

            content_hash = hashlib.sha256(data).hexdigest()
            meta: dict[str, Any] = {
                "page_count": page_count,
                "pages_with_text": pages_with_text,
                "original_filename": original_filename,
                "file_size": len(data),
            }
            return ExtractedDocument(
                text=full_text,
                content_hash=content_hash,
                source_uri=f"upload://{content_hash}",
                mime="application/pdf",
                raw_bytes=data,
                raw_filename="raw.pdf",
                metadata=meta,
            )
        finally:
            doc.close()
