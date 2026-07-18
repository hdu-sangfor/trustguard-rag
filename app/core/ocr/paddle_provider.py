"""本地 PaddleOCR Provider。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.ocr.errors import OcrError
from app.core.ocr.protocol import OcrRecognizeResult

logger = logging.getLogger(__name__)

_PADDLE_CACHE: Any = None


class PaddleOcrProvider:
    """基于 PaddleOCR 的本地识别（lazy import）。"""

    name = "paddle"

    def __init__(self, *, lang: str = "ch", use_angle_cls: bool = True) -> None:
        self._lang = lang
        self._use_angle_cls = use_angle_cls

    def _engine(self):
        global _PADDLE_CACHE
        if _PADDLE_CACHE is not None:
            return _PADDLE_CACHE
        try:
            from paddleocr import PaddleOCR
        except ImportError as e:
            raise OcrError(
                "PaddleOCR is not installed. "
                "Install with: pip install 'trustguard-rag[ocr-local]' "
                "or set RAG_OCR_PROVIDER=api|none"
            ) from e
        _PADDLE_CACHE = PaddleOCR(
            use_angle_cls=self._use_angle_cls,
            lang=self._lang,
            show_log=False,
        )
        return _PADDLE_CACHE

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        lang: str | None = None,
    ) -> OcrRecognizeResult:
        def _run() -> OcrRecognizeResult:
            import numpy as np
            from PIL import Image
            import io

            engine = self._engine()
            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            arr = np.array(image)
            raw = engine.ocr(arr, cls=True)
            lines: list[str] = []
            confidences: list[float] = []
            if raw:
                for block in raw:
                    if not block:
                        continue
                    for item in block:
                        if not item or len(item) < 2:
                            continue
                        text_info = item[1]
                        if isinstance(text_info, (list, tuple)) and text_info:
                            lines.append(str(text_info[0]))
                            if len(text_info) > 1:
                                try:
                                    confidences.append(float(text_info[1]))
                                except (TypeError, ValueError):
                                    pass
            text = "\n".join(line.strip() for line in lines if line and line.strip()).strip()
            conf = sum(confidences) / len(confidences) if confidences else None
            return OcrRecognizeResult(
                text=text,
                confidence=conf,
                raw={"lines": len(lines)},
                empty=not bool(text),
            )

        try:
            return await asyncio.to_thread(_run)
        except OcrError:
            raise
        except Exception as e:  # noqa: BLE001
            raise OcrError(f"PaddleOCR failed: {e}") from e
