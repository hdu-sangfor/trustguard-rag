from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.ingest.compensator import CleanupError, Compensator
from app.domain import DocumentStatus


class _DocumentStore:
    def __init__(self, status: DocumentStatus = DocumentStatus.READY) -> None:
        self.doc = SimpleNamespace(id="doc-1", status=status, blob_path="artifacts/doc-1/v1")

    async def get(self, document_id: str):
        return self.doc

    async def update_status(self, document_id: str, status: DocumentStatus) -> None:
        self.doc.status = status

    async def delete(self, document_id: str) -> bool:
        self.doc = None
        return True

    async def list_by_status(self, status: DocumentStatus):
        if self.doc and self.doc.status == status:
            return [self.doc]
        return []


def _build_compensator(*, qdrant_failure: bool = False, opensearch_failure: bool = False):
    documents = _DocumentStore()
    chunks = SimpleNamespace(
        point_ids_for_document=AsyncMock(return_value=["point-1"]),
        delete_for_document=AsyncMock(return_value=["point-1"]),
    )
    qdrant = SimpleNamespace(
        delete_document=AsyncMock(
            side_effect=RuntimeError("qdrant down") if qdrant_failure else None
        ),
        delete_points=AsyncMock(),
    )
    opensearch = SimpleNamespace(
        delete_for_document=AsyncMock(
            side_effect=RuntimeError("opensearch down") if opensearch_failure else None
        )
    )
    jobs = SimpleNamespace(clear_document_references=AsyncMock())
    blobs = SimpleNamespace(delete_prefix=MagicMock())
    compensator = Compensator(
        document_store=documents,
        job_store=jobs,
        chunk_store=chunks,
        blob_store=blobs,
        indexer=qdrant,
        opensearch_indexer=opensearch,
    )
    return compensator, documents, chunks, qdrant, opensearch


@pytest.mark.asyncio
async def test_delete_attempts_both_indexes_and_persists_deleting_on_failure() -> None:
    compensator, documents, chunks, qdrant, opensearch = _build_compensator(
        qdrant_failure=True
    )

    with pytest.raises(CleanupError) as exc_info:
        await compensator.delete_document("doc-1")

    assert exc_info.value.failures == ("qdrant",)
    assert documents.doc.status == DocumentStatus.DELETING
    opensearch.delete_for_document.assert_awaited_once_with("doc-1")
    chunks.delete_for_document.assert_not_awaited()

    qdrant.delete_document.side_effect = None
    assert await compensator.delete_document("doc-1") is True
    assert documents.doc is None
    assert chunks.delete_for_document.await_count == 1


@pytest.mark.asyncio
async def test_supersede_persists_intermediate_state_until_both_indexes_deleted() -> None:
    compensator, documents, chunks, _, opensearch = _build_compensator(
        opensearch_failure=True
    )

    with pytest.raises(CleanupError):
        await compensator.supersede_document("doc-1")

    assert documents.doc.status == DocumentStatus.SUPERSEDING
    chunks.delete_for_document.assert_not_awaited()

    opensearch.delete_for_document.side_effect = None
    await compensator.supersede_document("doc-1")

    assert documents.doc.status == DocumentStatus.SUPERSEDED
    assert chunks.delete_for_document.await_count == 1


@pytest.mark.asyncio
async def test_startup_recovery_resumes_persisted_deletion() -> None:
    compensator, documents, _, _, _ = _build_compensator()
    documents.doc.status = DocumentStatus.DELETING

    result = await compensator.resume_pending_cleanups()

    assert result == {"deleting": 1, "superseeding": 0, "failed": 0, "errors": 0}
    assert documents.doc is None
