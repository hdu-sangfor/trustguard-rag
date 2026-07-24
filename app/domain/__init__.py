"""领域模型。"""

from app.domain.answer import AnswerStatus
from app.domain.document import DELETABLE_DOCUMENT_STATUSES, DocumentStatus
from app.domain.ingest import (
    CANCELLABLE_JOB_STATUSES,
    INGEST_CLAIMABLE_STATUSES,
    RESOLVE_CLAIMABLE_STATUSES,
    RESUMABLE_JOB_STATUSES,
    TERMINAL_JOB_STATUSES,
    IngestJobStatus,
    IngestStep,
    PipelineResult,
)
from app.domain.ocr import OcrRegionStatus
from app.domain.retrieval import (
    EffectiveSearchMode,
    RetrievalComponent,
    SearchStatus,
)
from app.domain.worker import ClaimOutcome, CleanupAction, OutboxStatus

__all__ = [
    "AnswerStatus",
    "CANCELLABLE_JOB_STATUSES",
    "INGEST_CLAIMABLE_STATUSES",
    "RESOLVE_CLAIMABLE_STATUSES",
    "RESUMABLE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "ClaimOutcome",
    "CleanupAction",
    "DELETABLE_DOCUMENT_STATUSES",
    "DocumentStatus",
    "EffectiveSearchMode",
    "IngestJobStatus",
    "IngestStep",
    "OcrRegionStatus",
    "OutboxStatus",
    "PipelineResult",
    "RetrievalComponent",
    "SearchStatus",
]
