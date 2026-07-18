"""OCR 抽取文本合并约定（PDF / Word 共用）。"""
from __future__ import annotations


def ocr_image_prefix(image_no: int, *, page_no: int | None = None) -> str:
    """统一 OCR 正文前缀：`[OCR image N]` 或 `[OCR image N p{page}]`。"""
    if page_no is not None:
        return f"[OCR image {image_no} p{page_no}]"
    return f"[OCR image {image_no}]"


def format_ocr_span(image_no: int, ocr_text: str, *, page_no: int | None = None) -> str:
    """有内容的 OCR 片段：前缀 + 文本；空文本返回空串（不写入正文）。"""
    text = (ocr_text or "").strip()
    if not text:
        return ""
    return f"{ocr_image_prefix(image_no, page_no=page_no)}\n{text}"
