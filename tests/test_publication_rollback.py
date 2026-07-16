"""Qdrant 索引失败时的发布回滚测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.embedding.client import EmbeddingClient, EmbeddingError
from app.core.ingest.errors import EMBEDDING_FAILED, INDEX_FAILED, IngestError
from app.domain import DocumentStatus
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from pdf_fixtures import make_pdf_bytes


class FailingIndexer:
    def __init__(self) -> None:
        self.deleted_documents: list[str] = []

    async def ensure_collection(self) -> None:
        return None

    async def upsert_chunks(self, **kwargs) -> None:
        raise IngestError(INDEX_FAILED, "qdrant down")

    async def delete_points(self, point_ids: list[str]) -> None:
        return None

    async def delete_document(self, document_id: str) -> None:
        self.deleted_documents.append(document_id)


class RecordingIndexer(FailingIndexer):
    def __init__(self) -> None:
        super().__init__()
        self.upserted_documents: list[str] = []

    async def upsert_chunks(self, **kwargs) -> None:
        self.upserted_documents.append(kwargs["document_id"])


class FailingOpenSearchIndexer:
    def __init__(self) -> None:
        self.deleted_documents: list[str] = []

    async def ensure_index(self) -> None:
        raise RuntimeError("opensearch down")

    async def index_chunks(self, *args, **kwargs) -> None:
        raise AssertionError("index_chunks should not run when ensure_index fails")

    async def delete_for_document(self, document_id: str) -> None:
        self.deleted_documents.append(document_id)


@pytest.mark.asyncio
async def test_publication_rollback_on_index_failure(
    client: AsyncClient, tmp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexer = FailingIndexer()
    monkeypatch.setattr(
        "app.core.ingest.pipeline.get_qdrant_indexer",
        lambda: indexer,
    )

    pdf = make_pdf_bytes(["Rollback test content"])
    resp = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("rollback.pdf", pdf, "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    job = (await client.get(f"/v1/ingest/jobs/{job_id}")).json()
    assert job["status"] == "ingest_retrying"
    assert job["error_code"] == INDEX_FAILED

    ds = DocumentStore()
    docs = await ds.list_by_status(DocumentStatus.READY)
    assert not docs
    failed_docs = await ds.list_by_status(DocumentStatus.FAILED)
    assert indexer.deleted_documents == [failed_docs[0].id]

    staging = tmp_storage / "staging" / "jobs" / job_id
    assert staging.exists()  # 可重试任务保留源上传文件，供下一次投递使用。


@pytest.mark.asyncio
async def test_rollback_deletes_vectors_when_chunk_insert_fails(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    indexer = RecordingIndexer()
    monkeypatch.setattr(
        "app.core.ingest.pipeline.get_qdrant_indexer",
        lambda: indexer,
    )

    async def fail_chunk_insert(self, chunks) -> None:
        raise RuntimeError("database write failed")

    monkeypatch.setattr(ChunkStore, "create_many", fail_chunk_insert)

    pdf = make_pdf_bytes(["Vector cleanup after chunk failure"])
    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("orphan.pdf", pdf, "application/pdf")},
    )
    job = (await client.get(f"/v1/ingest/jobs/{response.json()['job_id']}")).json()

    assert job["status"] == "ingest_retrying"
    assert indexer.deleted_documents == indexer.upserted_documents


@pytest.mark.asyncio
async def test_opensearch_failure_rolls_back_qdrant_and_never_publishes_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    qdrant = RecordingIndexer()
    opensearch = FailingOpenSearchIndexer()
    monkeypatch.setattr("app.core.ingest.pipeline.get_qdrant_indexer", lambda: qdrant)
    monkeypatch.setattr(
        "app.core.ingest.pipeline.get_opensearch_indexer", lambda: opensearch
    )

    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={
            "file": (
                "opensearch-failure.pdf",
                make_pdf_bytes(["Must not be published partially"]),
                "application/pdf",
            )
        },
    )
    job = (await client.get(f"/v1/ingest/jobs/{response.json()['job_id']}")).json()

    assert job["status"] == "ingest_retrying"
    assert job["error_code"] == INDEX_FAILED
    assert await DocumentStore().list_by_status(DocumentStatus.READY) == []
    failed = await DocumentStore().list_by_status(DocumentStatus.FAILED)
    assert qdrant.upserted_documents == [failed[0].id]
    assert qdrant.deleted_documents == [failed[0].id]
    assert opensearch.deleted_documents == [failed[0].id]
    assert await ChunkStore().list_for_document(failed[0].id) == []


@pytest.mark.asyncio
async def test_non_retryable_embedding_error_fails_immediately(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_embedding(self, texts):
        raise EmbeddingError("invalid embedding request", retryable=False)

    monkeypatch.setattr(EmbeddingClient, "embed_texts", fail_embedding)
    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={
            "file": (
                "invalid-embedding.pdf",
                make_pdf_bytes(["Embedding request should fail once"]),
                "application/pdf",
            )
        },
    )

    job = (await client.get(f"/v1/ingest/jobs/{response.json()['job_id']}")).json()
    assert job["status"] == "failed"
    assert job["attempt"] == 1
    assert job["error_code"] == EMBEDDING_FAILED
