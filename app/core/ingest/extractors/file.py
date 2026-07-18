"""带 MIME 路由的文件上传抽取器。"""
from __future__ import annotations

import mimetypes
from pathlib import PurePath

from app.core.ingest.errors import UNSUPPORTED_MIME, IngestError
from app.core.ingest.extractors.mineru import (
    DOCX_MIME,
    PDF_MIME,
    MineruDocxExtractor,
    MineruPdfExtractor,
)
from app.core.ingest.extractors.text import TextExtractor
from app.core.ingest.models import ExtractedDocument

MIME_ROUTER: dict[str, object] = {
    PDF_MIME: MineruPdfExtractor(),
    "text/plain": TextExtractor(mime_type="text/plain", raw_filename="raw.txt"),
    "text/markdown": TextExtractor(mime_type="text/markdown", raw_filename="raw.md"),
    "text/x-markdown": TextExtractor(mime_type="text/markdown", raw_filename="raw.md"),
    DOCX_MIME: MineruDocxExtractor(),
}


def _guess_mime(filename: str, data: bytes) -> str:
    """优先根据文件签名推断 MIME 类型，再回退到文件名。"""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".txt":
        return "text/plain"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix == ".docx":
        return DOCX_MIME
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


class FileExtractor:
    def _resolve_extractor(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None,
    ) -> object:
        """Resolve the configured extractor without depending on OS MIME registration."""
        guessed_mime = _guess_mime(original_filename, data)
        if data.startswith(b"%PDF-"):
            resolved_mime = "application/pdf"
        elif mime in MIME_ROUTER:
            resolved_mime = mime
        elif guessed_mime in MIME_ROUTER:
            resolved_mime = guessed_mime
        else:
            resolved_mime = mime or guessed_mime
        extractor = MIME_ROUTER.get(resolved_mime)
        if extractor is None:
            raise IngestError(UNSUPPORTED_MIME, f"Unsupported MIME type: {resolved_mime}")
        return extractor

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None = None,
    ) -> ExtractedDocument:
        """将上传字节路由到解析后 MIME 类型对应的抽取器。"""
        extractor = self._resolve_extractor(
            data,
            original_filename=original_filename,
            mime=mime,
        )
        if not hasattr(extractor, "extract"):
            raise RuntimeError("This document type requires extract_async()")
        return extractor.extract(  # type: ignore[union-attr]
            data,
            original_filename=original_filename,
        )

    async def extract_async(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None = None,
    ) -> ExtractedDocument:
        """Route local formats synchronously and remote parsers asynchronously."""
        extractor = self._resolve_extractor(
            data,
            original_filename=original_filename,
            mime=mime,
        )
        if hasattr(extractor, "extract_async"):
            return await extractor.extract_async(  # type: ignore[union-attr]
                data,
                original_filename=original_filename,
            )
        return extractor.extract(  # type: ignore[union-attr]
            data,
            original_filename=original_filename,
        )
