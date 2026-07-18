"""入库任务的领域状态、步骤和执行结果。"""

from __future__ import annotations

from enum import StrEnum


class IngestJobStatus(StrEnum):
    """持久化到 `ingest_jobs` 表的任务生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    CONFLICT = "conflict"
    RESOLVING = "resolving"
    INGEST_RETRYING = "ingest_retrying"
    RESOLVE_RETRYING = "resolve_retrying"
    SUCCEEDED = "succeeded"
    DEDUPLICATED = "deduplicated"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DISCARDED = "discarded"


class IngestStep(StrEnum):
    """持久化到 `current_step` 字段和步骤日志的入库步骤。"""

    QUEUED = "queued"
    RECOVER = "recover"
    VALIDATE = "validate"
    EXTRACT = "extract"
    DEDUP = "dedup"
    CONFLICT_CHECK = "conflict_check"
    COMMIT_ARTIFACTS = "commit_artifacts"
    CHUNK = "chunk"
    EMBED = "embed"
    INDEX = "index"
    OPENSEARCH_INDEX = "opensearch_index"
    PUBLISH = "publish"
    RETRY_WAIT = "retry_wait"
    RESOLVE = "resolve"
    RESOLVE_SUPERSEDE = "resolve_supersede"
    SUPERSEDE_CLEANUP = "supersede_cleanup"
    RESOLVE_PUBLISH = "resolve_publish"
    RESOLVE_DISCARD = "resolve_discard"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PipelineResult(StrEnum):
    """入库流水线方法的返回值；与持久化任务状态分开建模。"""

    MISSING = "missing"
    SUCCEEDED = "succeeded"
    DEDUPLICATED = "deduplicated"
    CONFLICT = "conflict"
    RETRYING = "retrying"
    FAILED = "failed"
    DISCARDED = "discarded"


INGEST_CLAIMABLE_STATUSES = (
    IngestJobStatus.QUEUED,
    IngestJobStatus.INGEST_RETRYING,
)

RESOLVE_CLAIMABLE_STATUSES = (
    IngestJobStatus.RESOLVING,
    IngestJobStatus.RESOLVE_RETRYING,
)

RESUMABLE_JOB_STATUSES = (
    IngestJobStatus.QUEUED,
    IngestJobStatus.RUNNING,
    IngestJobStatus.CONFLICT,
    IngestJobStatus.RESOLVING,
    IngestJobStatus.INGEST_RETRYING,
    IngestJobStatus.RESOLVE_RETRYING,
)

CANCELLABLE_JOB_STATUSES = RESUMABLE_JOB_STATUSES

TERMINAL_JOB_STATUSES = frozenset(
    {
        IngestJobStatus.SUCCEEDED,
        IngestJobStatus.DEDUPLICATED,
        IngestJobStatus.FAILED,
        IngestJobStatus.CANCELLED,
        IngestJobStatus.DISCARDED,
    }
)
