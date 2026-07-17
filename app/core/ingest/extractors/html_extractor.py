"""简易 HTML 正文抽取。"""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser

from app.core.ingest.errors import CORRUPT_FILE, EMPTY_CONTENT, FILE_TOO_LARGE, IngestError
from app.core.ingest.models import ExtractedDocument
from app.settings import get_settings

_SKIP = {"script", "style", "noscript", "svg"}


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag.lower() in _SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._chunks.append(text + " ")

    def text(self) -> str:
        joined = "".join(self._chunks)
        return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", joined)).strip()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError(CORRUPT_FILE, "Unable to decode HTML file")


class HtmlExtractor:
    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.html",
    ) -> ExtractedDocument:
        settings = get_settings()
        if len(data) > settings.ingest_max_file_bytes:
            raise IngestError(FILE_TOO_LARGE, "File exceeds max size")
        parser = _TextCollector()
        try:
            parser.feed(_decode(data))
            parser.close()
        except Exception as e:  # noqa: BLE001
            raise IngestError(CORRUPT_FILE, f"Invalid HTML: {e}") from e
        text = parser.text()
        if not text:
            raise IngestError(EMPTY_CONTENT, "HTML has no extractable text")
        content_hash = hashlib.sha256(data).hexdigest()
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime="text/html",
            raw_bytes=data,
            raw_filename="raw.html",
            metadata={
                "original_filename": original_filename,
                "file_size": len(data),
            },
        )
