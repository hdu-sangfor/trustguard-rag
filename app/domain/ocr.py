"""OCR 区域状态。"""
from __future__ import annotations

from enum import StrEnum


class OcrRegionStatus(StrEnum):
    PENDING = "pending"
    EMPTY = "empty"
    FAILED = "failed"
    APPROVED = "approved"
    CORRECTED = "corrected"
