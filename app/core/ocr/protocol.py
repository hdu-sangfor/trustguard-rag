"""OCR 识别结果与 Provider 协议。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class OcrRecognizeResult:
    """单次 OCR 识别输出。"""

    text: str
    confidence: float | None = None
    raw: dict[str, Any] | None = None
    empty: bool = False


@dataclass
class OcrRegionDraft:
    """抽取阶段产生的 OCR 区域草稿（尚未绑定文档 ID）。"""

    page_no: int | None
    bbox: list[float]  # [x0, y0, x1, y1]
    crop_png: bytes
    ocr_text: str
    status: str  # pending | empty | failed
    provider: str
    confidence: float | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class OcrProvider(Protocol):
    """OCR 提供方协议。"""

    name: str

    async def recognize(
        self,
        image_bytes: bytes,
        *,
        lang: str | None = None,
    ) -> OcrRecognizeResult:
        """识别图片中的文字。"""
        ...
