"""按 MIME / 魔数路由到具体抽取器。"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from app.core.ingest.errors import UNSUPPORTED_MIME, IngestError
from app.core.ingest.extractors.html_extractor import HtmlExtractor
from app.core.ingest.extractors.image_extractor import ImageExtractor
from app.core.ingest.extractors.pdf import PdfExtractor
from app.core.ingest.extractors.text_extractor import (
    CsvExtractor,
    JsonExtractor,
    MarkdownExtractor,
    PlainTextExtractor,
)
from app.core.ingest.models import ExtractedDocument

_pdf = PdfExtractor()
_plain = PlainTextExtractor()
_md = MarkdownExtractor()
_csv = CsvExtractor()
_json = JsonExtractor()
_html = HtmlExtractor()
_image = ImageExtractor()

MIME_ROUTER: dict[str, object] = {
    "application/pdf": _pdf,
    "text/plain": _plain,
    "text/markdown": _md,
    "text/x-markdown": _md,
    "text/csv": _csv,
    "application/json": _json,
    "text/html": _html,
    "application/xhtml+xml": _html,
    "image/png": _image,
    "image/jpeg": _image,
    "image/webp": _image,
    "image/gif": _image,
    "image/bmp": _image,
    "image/tiff": _image,
}

SUPPORTED_MIME_TYPES = sorted(MIME_ROUTER.keys())

_EXT_MIME = {
    ".pdf": "application/pdf",
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
        return "application/pdf"
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
    lower = filename.lower()
    if "<html" in data[:2000].decode("utf-8", errors="ignore").lower():
        return "text/html"
    ext = Path(lower).suffix
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
        resolved_mime = mime or _guess_mime(original_filename, data)
        # 客户端 MIME 不可靠时，魔数优先
        magic_mime = _guess_mime(original_filename, data)
        if magic_mime in MIME_ROUTER:
            resolved_mime = magic_mime
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
                data,
                original_filename=original_filename,
                mime=mime,
            )
        if mime in {"text/plain"}:
            return extractor.extract(  # type: ignore[union-attr]
                data,
                original_filename=original_filename,
                mime=mime,
            )
        if mime in {"text/markdown", "text/x-markdown"}:
            return extractor.extract(  # type: ignore[union-attr]
                data,
                original_filename=original_filename,
                mime="text/markdown",
            )
        return extractor.extract(  # type: ignore[union-attr]
            data,
            original_filename=original_filename,
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
                    data,
                    original_filename=original_filename,
                    mime=mime,
                )
            return await extractor.extract_async(  # type: ignore[union-attr]
                data,
                original_filename=original_filename,
            )
        return self._dispatch_sync(
            extractor, data, original_filename=original_filename, mime=mime
        )
