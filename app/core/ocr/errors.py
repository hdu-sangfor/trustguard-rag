"""OCR 错误。"""
from __future__ import annotations


class OcrError(RuntimeError):
    """OCR 提供方不可用或调用失败。"""
