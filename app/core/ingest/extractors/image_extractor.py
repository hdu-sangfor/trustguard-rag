"""整图 OCR 入库。"""
from __future__ import annotations

import hashlib

from app.core.ingest.errors import FILE_TOO_LARGE, OCR_FAILED, OCR_UNAVAILABLE, IngestError
from app.core.ingest.extractors._async_utils import run_sync
from app.core.ingest.models import ExtractedDocument
from app.core.ocr import get_ocr_engine
from app.core.ocr.errors import OcrError
from app.core.ocr.protocol import OcrRegionDraft
from app.settings import get_settings

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}


class ImageExtractor:
    """对整张图片做 OCR，生成 ExtractedDocument。"""

    def __init__(self, ocr_engine=None) -> None:
        self._ocr = ocr_engine

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "image.png",
        mime: str = "image/png",
    ) -> ExtractedDocument:
        return run_sync(
            self.extract_async(data, original_filename=original_filename, mime=mime)
        )

    async def extract_async(
        self,
        data: bytes,
        *,
        original_filename: str = "image.png",
        mime: str = "image/png",
    ) -> ExtractedDocument:
        settings = get_settings()
        if len(data) > settings.ingest_max_file_bytes:
            raise IngestError(FILE_TOO_LARGE, "File exceeds max size")

        engine = self._ocr or get_ocr_engine()
        if not engine.enabled:
            raise IngestError(
                OCR_UNAVAILABLE,
                "Image ingest requires OCR. Set RAG_OCR_PROVIDER=local|api",
            )

        try:
            result = await engine.recognize(data)
        except OcrError as e:
            raise IngestError(OCR_UNAVAILABLE, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise IngestError(OCR_FAILED, str(e)) from e

        text = (result.text or "").strip()
        status = "empty" if not text else "pending"
        draft = OcrRegionDraft(
            page_no=1,
            bbox=[0.0, 0.0, 0.0, 0.0],
            crop_png=data,
            ocr_text=text,
            status=status,
            provider=engine.provider_name,
            confidence=result.confidence,
        )
        content_hash = hashlib.sha256(data).hexdigest()
        ext = _EXT_BY_MIME.get(mime, "bin")
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime=mime,
            raw_bytes=data,
            raw_filename=f"raw.{ext}",
            metadata={
                "original_filename": original_filename,
                "file_size": len(data),
                "ocr_region_drafts": [draft],
            },
        )
