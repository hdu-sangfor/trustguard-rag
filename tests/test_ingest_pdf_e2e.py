"""通过 API 执行本地 PDF 入库的端到端测试。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from pdf_fixtures import make_pdf_bytes


@pytest.mark.asyncio
async def test_ingest_pdf_e2e(client: AsyncClient, tmp_storage) -> None:
    pdf = make_pdf_bytes(["Alpha content", "Beta content"])
    resp = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("report.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    job_resp = await client.get(f"/v1/ingest/jobs/{job_id}")
    assert job_resp.status_code == 200
    job = job_resp.json()
    assert job["status"] == "succeeded"
    document_id = job["document_id"]
    assert document_id

    doc_resp = await client.get(f"/v1/documents/{document_id}")
    assert doc_resp.json()["status"] == "ready"

    chunks_resp = await client.get(f"/v1/documents/{document_id}/chunks")
    chunks = chunks_resp.json()
    assert len(chunks) >= 1
    assert all(c.get("page_no") is not None for c in chunks)

    artifacts_resp = await client.get(f"/v1/documents/{document_id}/artifacts")
    files = artifacts_resp.json()["files"]
    assert "raw.pdf" in files
    assert "extracted.txt" in files
    assert "meta.json" in files

    bundle = tmp_storage / "artifacts" / document_id / "v1"
    assert (bundle / "raw.pdf").exists()
    assert (bundle / "extracted.txt").read_text(encoding="utf-8")
