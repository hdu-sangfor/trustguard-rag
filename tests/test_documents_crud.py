"""文档知识库 CRUD API 的集成测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.domain import DocumentStatus
from app.stores.blob_store import BlobStore
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore


async def _create_document(
    *,
    filename: str,
    title: str | None = None,
    status: DocumentStatus = DocumentStatus.READY,
):
    store = DocumentStore()
    return await store.create(
        source_type="file",
        source_uri=f"upload://{filename}",
        content_hash=uuid4().hex * 2,
        status=status,
        title=title,
        mime_type="application/pdf",
        original_filename=filename,
        metadata={"page_count": 2},
    )


@pytest.mark.asyncio
async def test_list_search_and_update_documents(client: AsyncClient) -> None:
    first = await _create_document(filename="security-guide.pdf", title="安全指南")
    await _create_document(filename="operations.pdf", title="运维手册")

    response = await client.get("/v1/documents", params={"q": "安全", "status": "ready"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == first.id
    assert payload["items"][0]["title"] == "安全指南"

    response = await client.patch(
        f"/v1/documents/{first.id}",
        json={"title": "企业安全指南", "metadata": {"owner": "security"}},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "企业安全指南"
    assert response.json()["metadata"] == {"owner": "security"}

    detail = await client.get(f"/v1/documents/{first.id}")
    assert detail.json()["title"] == "企业安全指南"


@pytest.mark.asyncio
async def test_update_rejects_empty_payload_and_blank_title(client: AsyncClient) -> None:
    document = await _create_document(filename="validation.pdf")

    assert (await client.patch(f"/v1/documents/{document.id}", json={})).status_code == 400
    assert (
        await client.patch(f"/v1/documents/{document.id}", json={"title": "   "})
    ).status_code == 422


@pytest.mark.asyncio
async def test_delete_cleans_artifacts_chunks_and_document(
    client: AsyncClient, tmp_storage
) -> None:
    document = await _create_document(filename="delete-me.pdf")
    blobs = BlobStore()
    blob_path = blobs.commit_bundle(
        document.id,
        raw_name="delete-me.pdf",
        raw_bytes=b"pdf",
        extracted_text="content",
        meta={"page_count": 1},
    )
    await DocumentStore().update_status(document.id, DocumentStatus.READY, blob_path=blob_path)

    chunk_id = str(uuid4())
    await ChunkStore().create_many(
        [
            {
                "id": chunk_id,
                "document_id": document.id,
                "chunk_index": 0,
                "text": "content",
                "token_count": 1,
                "qdrant_point_id": chunk_id,
            }
        ]
    )
    job = await JobStore().create(source_type="file", source="delete-me.pdf")
    await JobStore().finish(
        job.id,
        "conflict",
        document_id=document.id,
        pending_document_id=document.id,
        conflict_candidates=[document.id, "other-document"],
    )
    assert blobs.artifact_dir(document.id).exists()

    response = await client.delete(f"/v1/documents/{document.id}")
    assert response.status_code == 204
    assert await DocumentStore().get(document.id) is None
    assert await ChunkStore().list_for_document(document.id) == []
    cleaned_job = await JobStore().get(job.id)
    assert cleaned_job is not None
    assert cleaned_job.document_id is None
    assert cleaned_job.pending_document_id is None
    assert cleaned_job.conflict_candidates_json == ["other-document"]
    assert not blobs.artifact_dir(document.id).exists()
    assert (await client.get(f"/v1/documents/{document.id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_document_returns_404(client: AsyncClient) -> None:
    response = await client.delete(f"/v1/documents/{uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("document_status", [DocumentStatus.STAGING, DocumentStatus.INDEXING])
async def test_delete_rejects_documents_still_being_ingested(
    client: AsyncClient, document_status: DocumentStatus
) -> None:
    document = await _create_document(
        filename=f"{document_status}.pdf",
        status=document_status,
    )

    response = await client.delete(f"/v1/documents/{document.id}")

    assert response.status_code == 409
    assert await DocumentStore().get(document.id) is not None


@pytest.mark.asyncio
async def test_list_rejects_unknown_document_status(client: AsyncClient) -> None:
    response = await client.get("/v1/documents", params={"status": "unknown"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_hides_internal_cleanup_error(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = await _create_document(filename="cleanup-error.pdf")

    class _FailingCompensator:
        async def delete_document(self, document_id: str) -> bool:
            raise RuntimeError("secret storage endpoint")

    monkeypatch.setattr("app.api.documents.get_compensator", lambda: _FailingCompensator())

    response = await client.delete(f"/v1/documents/{document.id}")

    assert response.status_code == 502
    assert "secret storage endpoint" not in response.text
    assert "reference=" in response.json()["detail"]
    assert await DocumentStore().get(document.id) is not None
