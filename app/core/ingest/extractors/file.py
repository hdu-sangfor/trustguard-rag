"""带 MIME 路由的文件上传抽取器。"""
from __future__ import annotations

import mimetypes

from app.core.ingest.errors import UNSUPPORTED_MIME, IngestError
from app.core.ingest.extractors.pdf import PdfExtractor
from app.core.ingest.models import ExtractedDocument

MIME_ROUTER: dict[str, object] = {
    "application/pdf": PdfExtractor(),
}


def _guess_mime(filename: str, data: bytes) -> str:
    """优先根据文件签名推断 MIME 类型，再回退到文件名。"""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


class FileExtractor:
    def extract(
        self,
        data: bytes,
        *,
        original_filename: str,
        mime: str | None = None,
    ) -> ExtractedDocument:
        """将上传字节路由到解析后 MIME 类型对应的抽取器。"""
        resolved_mime = mime or _guess_mime(original_filename, data)
        extractor = MIME_ROUTER.get(resolved_mime)
        if extractor is None:
            raise IngestError(UNSUPPORTED_MIME, f"Unsupported MIME type: {resolved_mime}")
        return extractor.extract(  # type: ignore[union-attr]
            data,
            original_filename=original_filename,
        )
