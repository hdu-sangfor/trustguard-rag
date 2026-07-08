"""Publication rollback when Qdrant indexing fails."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.ingest.errors import INDEX_FAILED, IngestError
from app.stores.blob_store import BlobStore
from app.stores.document_store import DocumentStore
from pdf_fixtures import make_pdf_bytes


class FailingIndexer:
    async def ensure_collection(self) -> None:
        return None

    async def upsert_chunks(self, **kwargs) -> None:
        raise IngestError(INDEX_FAILED, "qdrant down")

    async def delete_points(self, point_ids: list[str]) -> None:
        return None


@pytest.mark.asyncio
async def test_publication_rollback_on_index_failure(
    client: AsyncClient, tmp_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.core.ingest.pipeline.get_qdrant_indexer",
        lambda: FailingIndexer(),
    )

    pdf = make_pdf_bytes(["Rollback test content"])
    resp = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("rollback.pdf", pdf, "application/pdf")},
    )
    job_id = resp.json()["job_id"]
    job = (await client.get(f"/v1/ingest/jobs/{job_id}")).json()
    assert job["status"] == "failed"
    assert job["error_code"] == INDEX_FAILED

    ds = DocumentStore()
    docs = await ds.list_by_status("ready")
    assert not docs

    bs = BlobStore()
    staging = tmp_storage / "staging" / "jobs" / job_id
    assert not staging.exists()
