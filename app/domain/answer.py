"""回答层使用的稳定领域值。"""

from __future__ import annotations

from enum import StrEnum


class AnswerStatus(StrEnum):
    """一次回答请求的业务结果。"""

    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
