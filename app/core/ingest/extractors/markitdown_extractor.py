"""MarkItDown-backed extractors for text-family local uploads."""

from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import PurePath
from typing import Any

from markitdown import MarkItDown

from app.core.ingest.errors import CORRUPT_FILE, EMPTY_CONTENT, FILE_TOO_LARGE, IngestError
from app.core.ingest.models import ExtractedDocument
from app.settings import get_settings

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

_MIME_EXTENSION: dict[str, str] = {
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
}

_RAW_FILENAME: dict[str, str] = {
    "text/plain": "raw.txt",
    "text/markdown": "raw.md",
    "text/x-markdown": "raw.md",
    "text/csv": "raw.csv",
    "application/json": "raw.json",
    "text/html": "raw.html",
    "application/xhtml+xml": "raw.html",
}

# Formats that are binary-safe to feed as UTF-8 after multi-encoding decode.


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError(CORRUPT_FILE, "Unable to decode text file")


def _strip_front_matter(text: str) -> tuple[str, dict[str, Any]]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return text, {}
    return text[match.end() :], {"front_matter": match.group(1).strip()}


class MarkItDownExtractor:
    """Convert text-family uploads via MarkItDown convert_stream only."""

    def __init__(self, *, engine: MarkItDown | None = None) -> None:
        self._engine = engine or MarkItDown(enable_plugins=False)

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.txt",
        mime: str = "text/plain",
    ) -> ExtractedDocument:
        settings = get_settings()
        if len(data) > settings.ingest_max_file_bytes:
            raise IngestError(FILE_TOO_LARGE, "File exceeds max size")
        if not data or not data.strip():
            raise IngestError(EMPTY_CONTENT, "Text file is empty")

        resolved_mime = mime if mime in _MIME_EXTENSION else "text/plain"
        extension = _MIME_EXTENSION[resolved_mime]
        suffix = PurePath(original_filename).suffix.lower()
        if suffix in {".txt", ".log", ".text", ".md", ".markdown", ".csv", ".json", ".html", ".htm"}:
            extension = ".md" if suffix == ".markdown" else (".html" if suffix == ".htm" else suffix)

        # Normalize encodings so GBK/BOM inputs stay readable for MarkItDown.
        try:
            stream_bytes = _decode_text(data).encode("utf-8")
        except IngestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IngestError(CORRUPT_FILE, f"Unable to prepare text for MarkItDown: {exc}") from exc

        try:
            result = self._engine.convert_stream(
                BytesIO(stream_bytes),
                file_extension=extension,
            )
        except IngestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IngestError(CORRUPT_FILE, f"MarkItDown failed: {exc}") from exc

        text = (getattr(result, "text_content", None) or getattr(result, "markdown", None) or "").strip()
        metadata: dict[str, Any] = {
            "original_filename": original_filename,
            "file_size": len(data),
            "parser": "markitdown",
            "extracted_format": "markdown",
        }

        if resolved_mime in {"text/markdown", "text/x-markdown"} or extension == ".md":
            text, front = _strip_front_matter(text)
            text = text.strip()
            metadata.update(front)

        if resolved_mime == "application/json":
            max_chars = settings.ingest_json_max_chars
            if len(text) > max_chars:
                text = text[:max_chars] + "\n...[truncated]"
                metadata["truncated"] = True
            else:
                metadata["truncated"] = False

        if not text:
            raise IngestError(EMPTY_CONTENT, "MarkItDown produced empty content")

        content_hash = hashlib.sha256(data).hexdigest()
        out_mime = "text/markdown" if resolved_mime == "text/x-markdown" else resolved_mime
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime=out_mime,
            raw_bytes=data,
            raw_filename=_RAW_FILENAME.get(resolved_mime, "raw.txt"),
            metadata=metadata,
        )
