"""按 MIME / 魔数路由到具体抽取器。"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from app.core.ingest.errors import UNSUPPORTED_MIME, IngestError
from app.core.ingest.extractors.image_extractor import ImageExtractor
from app.core.ingest.extractors.markitdown_extractor import MarkItDownExtractor
from app.core.ingest.extractors.mineru import (
    DOCX_MIME,
    PDF_MIME,
    MineruDocxExtractor,
    MineruPdfExtractor,
)
from app.core.ingest.extractors.pdf import PdfExtractor
from app.core.ingest.models import ExtractedDocument
from app.settings import get_settings

_local_pdf = PdfExtractor()
_mineru_pdf = MineruPdfExtractor()
_mineru_docx = MineruDocxExtractor()
_markitdown = MarkItDownExtractor()
_image = ImageExtractor()

_MARKITDOWN_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",
        "application/json",
        "text/html",
        "application/xhtml+xml",
    }
)

MIME_ROUTER: dict[str, object] = {
    PDF_MIME: _local_pdf,
    DOCX_MIME: _mineru_docx,
    "text/plain": _markitdown,
    "text/markdown": _markitdown,
    "text/x-markdown": _markitdown,
    "text/csv": _markitdown,
    "application/json": _markitdown,
    "text/html": _markitdown,
    "application/xhtml+xml": _markitdown,
    "image/png": _image,
    "image/jpeg": _image,
    "image/webp": _image,
    "image/gif": _image,
    "image/bmp": _image,
    "image/tiff": _image,
}

SUPPORTED_MIME_TYPES = sorted(MIME_ROUTER.keys())

_EXT_MIME = {
    ".pdf": PDF_MIME,
    ".docx": DOCX_MIME,
    ".txt": "text/plain",
    ".log": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def _guess_mime(filename: str, data: bytes) -> str:
    """优先根据文件签名推断 MIME 类型，再回退到扩展名 / mimetypes。"""
    if data.startswith(b"%PDF-"):
        return PDF_MIME
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip().startswith((b"{", b"[")):
        return "application/json"
    if "<html" in data[:2000].decode("utf-8", errors="ignore").lower():
        return "text/html"
    ext = Path(filename.lower()).suffix
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


class FileExtractor:
    def _resolve(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None = None,
    ) -> tuple[str, object]:
        guessed_mime = _guess_mime(original_filename, data)
        if guessed_mime in MIME_ROUTER:
            resolved_mime = guessed_mime
        elif mime in MIME_ROUTER:
            resolved_mime = mime
        else:
            resolved_mime = mime or guessed_mime

        if resolved_mime == PDF_MIME and get_settings().pdf_parser.strip().lower() == "mineru":
            return resolved_mime, _mineru_pdf

        extractor = MIME_ROUTER.get(resolved_mime)
        if extractor is None:
            raise IngestError(UNSUPPORTED_MIME, f"Unsupported MIME type: {resolved_mime}")
        return resolved_mime, extractor

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None = None,
    ) -> ExtractedDocument:
        resolved_mime, extractor = self._resolve(
            data, original_filename=original_filename, mime=mime
        )
        if not hasattr(extractor, "extract"):
            raise RuntimeError("This document type requires extract_async()")
        return self._dispatch_sync(
            extractor, data, original_filename=original_filename, mime=resolved_mime
        )

    async def extract_async(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None = None,
    ) -> ExtractedDocument:
        resolved_mime, extractor = self._resolve(
            data, original_filename=original_filename, mime=mime
        )
        return await self._dispatch_async(
            extractor, data, original_filename=original_filename, mime=resolved_mime
        )

    def _dispatch_sync(
        self,
        extractor: object,
        data: bytes,
        *,
        original_filename: str,
        mime: str,
    ) -> ExtractedDocument:
        if mime.startswith("image/"):
            return extractor.extract(  # type: ignore[union-attr]
                data, original_filename=original_filename, mime=mime
            )
        if mime in _MARKITDOWN_MIMES:
            return extractor.extract(  # type: ignore[union-attr]
                data, original_filename=original_filename, mime=mime
            )
        return extractor.extract(  # type: ignore[union-attr]
            data, original_filename=original_filename
        )

    async def _dispatch_async(
        self,
        extractor: object,
        data: bytes,
        *,
        original_filename: str,
        mime: str,
    ) -> ExtractedDocument:
        if hasattr(extractor, "extract_async"):
            if mime.startswith("image/"):
                return await extractor.extract_async(  # type: ignore[union-attr]
                    data, original_filename=original_filename, mime=mime
                )
            return await extractor.extract_async(  # type: ignore[union-attr]
                data, original_filename=original_filename
            )
        return self._dispatch_sync(extractor, data, original_filename=original_filename, mime=mime)
