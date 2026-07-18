"""UTF-8 plain-text and Markdown document extractors."""

from __future__ import annotations

import hashlib
from typing import Any

from app.core.ingest.errors import EMPTY_CONTENT, UNSUPPORTED_ENCODING, IngestError
from app.core.ingest.models import ExtractedDocument


class TextExtractor:
    """Decode a UTF-8 text document into the common ingest model."""

    def __init__(self, *, mime_type: str, raw_filename: str) -> None:
        self._mime_type = mime_type
        self._raw_filename = raw_filename

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.txt",
    ) -> ExtractedDocument:
        """Decode text, reject empty content, and attach stable metadata."""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IngestError(
                UNSUPPORTED_ENCODING,
                "Text file must use UTF-8 encoding",
            ) from exc

        text = text.strip()
        if not text:
            raise IngestError(EMPTY_CONTENT, "Text file is empty")

        content_hash = hashlib.sha256(data).hexdigest()
        metadata: dict[str, Any] = {
            "original_filename": original_filename,
            "file_size": len(data),
            "encoding": "utf-8",
            "parser": "utf8-passthrough",
        }
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime=self._mime_type,
            raw_bytes=data,
            raw_filename=self._raw_filename,
            metadata=metadata,
        )
