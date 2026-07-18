"""文件名冲突检测与解决测试。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.domain import DocumentStatus
from app.stores.document_store import DocumentStore
from pdf_fixtures import make_pdf_bytes


class _FailingOpenSearchIndexer:
    async def ensure_index(self) -> None:
        raise RuntimeError("opensearch down")

    async def index_chunks(self, *args, **kwargs) -> None:
        raise AssertionError("unexpected index call")

    async def delete_for_document(self, document_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_filename_conflict_resolve_keep_new(client: AsyncClient) -> None:
    pdf1 = make_pdf_bytes(["Version one text"])
    pdf2 = make_pdf_bytes(["Version two different"])

    r1 = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("same-name.pdf", pdf1, "application/pdf")},
    )
    job1 = (await client.get(f"/v1/ingest/jobs/{r1.json()['job_id']}")).json()
    assert job1["status"] == "succeeded"
    old_doc_id = job1["document_id"]

    r2 = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("same-name.pdf", pdf2, "application/pdf")},
    )
    job2_id = r2.json()["job_id"]
    job2 = (await client.get(f"/v1/ingest/jobs/{job2_id}")).json()
    assert job2["status"] == "conflict"
    pending_id = job2["pending_document_id"]
    assert old_doc_id in job2["conflict_candidates"]

    resolved = await client.post(
        f"/v1/ingest/jobs/{job2_id}/resolve",
        json={"keep_document_id": pending_id},
    )
    assert resolved.status_code == 202
    assert resolved.json()["status"] == "succeeded"

    old_doc = (await client.get(f"/v1/documents/{old_doc_id}")).json()
    assert old_doc["status"] == "superseded"

    new_doc_id = resolved.json()["document_id"]
    new_doc = (await client.get(f"/v1/documents/{new_doc_id}")).json()
    assert new_doc["status"] == "ready"


@pytest.mark.asyncio
async def test_deduplicated_upload(client: AsyncClient) -> None:
    pdf = make_pdf_bytes(["Dedup content"])
    r1 = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("a.pdf", pdf, "application/pdf")},
    )
    job1 = (await client.get(f"/v1/ingest/jobs/{r1.json()['job_id']}")).json()
    doc1 = job1["document_id"]

    r2 = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("b.pdf", pdf, "application/pdf")},
    )
    job2 = (await client.get(f"/v1/ingest/jobs/{r2.json()['job_id']}")).json()
    assert job2["status"] == "deduplicated"
    assert job2["document_id"] == doc1


@pytest.mark.asyncio
async def test_conflict_new_publish_failure_keeps_old_document_ready(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("protected.pdf", make_pdf_bytes(["old"]), "application/pdf")},
    )
    first_job = (await client.get(f"/v1/ingest/jobs/{first.json()['job_id']}")).json()
    old_id = first_job["document_id"]

    second = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("protected.pdf", make_pdf_bytes(["new"]), "application/pdf")},
    )
    second_job_id = second.json()["job_id"]
    conflict = (await client.get(f"/v1/ingest/jobs/{second_job_id}")).json()
    pending_id = conflict["pending_document_id"]
    monkeypatch.setattr(
        "app.core.ingest.pipeline.get_opensearch_indexer",
        lambda: _FailingOpenSearchIndexer(),
    )

    resolved = await client.post(
        f"/v1/ingest/jobs/{second_job_id}/resolve",
        json={"keep_document_id": pending_id},
    )

    assert resolved.status_code == 202
    assert resolved.json()["status"] == "resolve_retrying"
    assert (await DocumentStore().get(old_id)).status == DocumentStatus.READY
    assert (await DocumentStore().get(pending_id)).status == DocumentStatus.FAILED
