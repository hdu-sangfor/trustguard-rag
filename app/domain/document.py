"""文档领域类型。"""

from __future__ import annotations

from enum import StrEnum


class DocumentStatus(StrEnum):
    """知识库文档的生命周期状态。"""

    STAGING = "staging"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    SUPERSEDING = "superseeding"
    SUPERSEDED = "superseded"


DELETABLE_DOCUMENT_STATUSES = frozenset(
    {
        DocumentStatus.STAGING,
        DocumentStatus.INDEXING,
        DocumentStatus.READY,
        DocumentStatus.FAILED,
        DocumentStatus.DELETING,
        DocumentStatus.SUPERSEDED,
    }
)
