"""PDF ingest end-to-end via API."""
from __future__ import annotations

import httpx
import pytest
from httpx import AsyncClient

from app.core.ingest.extractors.file import MIME_ROUTER
from app.core.ingest.extractors.mineru import PDF_MIME, MineruPdfExtractor
from app.settings import Settings
from pdf_fixtures import make_pdf_bytes


@pytest.mark.asyncio
async def test_ingest_pdf_e2e(
    client: AsyncClient,
    tmp_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mineru_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": {
                    "report": {"md_content": "# PDF 文档\n\nAlpha content\n\nBeta content"}
                }
            },
        )

    settings = Settings(
        mineru_base_url="http://mineru.test:8000",
        mineru_backend="pipeline",
    )
    monkeypatch.setitem(
        MIME_ROUTER,
        PDF_MIME,
        MineruPdfExtractor(settings, transport=httpx.MockTransport(mineru_handler)),
    )
    pdf = make_pdf_bytes(["Alpha content", "Beta content"])
    resp = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("report.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    job_resp = await client.get(f"/v1/ingest/jobs/{job_id}")
    assert job_resp.status_code == 200
    job = job_resp.json()
    assert job["status"] == "succeeded"
    document_id = job["document_id"]
    assert document_id

    doc_resp = await client.get(f"/v1/documents/{document_id}")
    assert doc_resp.json()["status"] == "ready"
    assert doc_resp.json()["metadata"]["parser"] == "mineru"

    chunks_resp = await client.get(f"/v1/documents/{document_id}/chunks")
    chunks = chunks_resp.json()
    assert len(chunks) >= 1
    assert "Alpha content" in "\n".join(c["text"] for c in chunks)

    artifacts_resp = await client.get(f"/v1/documents/{document_id}/artifacts")
    files = artifacts_resp.json()["files"]
    assert "raw.pdf" in files
    assert "extracted.txt" in files
    assert "meta.json" in files

    bundle = tmp_storage / "artifacts" / document_id / "v1"
    assert (bundle / "raw.pdf").exists()
    assert (bundle / "extracted.txt").read_text(encoding="utf-8")
