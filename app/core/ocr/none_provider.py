"""禁用 OCR 时的空实现。"""
from __future__ import annotations

from app.core.ocr.protocol import OcrRecognizeResult


class NoneOcrProvider:
    """明确关闭 OCR：返回空文本。"""

    name = "none"

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        lang: str | None = None,
    ) -> OcrRecognizeResult:
        return OcrRecognizeResult(text="", confidence=None, raw=None, empty=True)
