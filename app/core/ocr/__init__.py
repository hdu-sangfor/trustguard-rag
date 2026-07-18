"""OCR 子系统。"""

from app.core.ocr.factory import OcrEngine, build_ocr_provider, get_ocr_engine, reset_ocr_engine_cache
from app.core.ocr.protocol import OcrRecognizeResult, OcrRegionDraft

__all__ = [
    "OcrEngine",
    "OcrRecognizeResult",
    "OcrRegionDraft",
    "build_ocr_provider",
    "get_ocr_engine",
    "reset_ocr_engine_cache",
]
