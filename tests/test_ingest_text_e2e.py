"""Plain-text and Markdown ingest end-to-end tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "upload_mime", "expected_mime", "raw_filename", "content"),
    [
        (
            "security.txt",
            "text/plain",
            "text/plain",
            "raw.txt",
            "TrustGuard 纯文本安全知识。",
        ),
        (
            "security.md",
            "application/octet-stream",
            "text/markdown",
            "raw.md",
            "# TrustGuard\n\nMarkdown 安全知识。",
        ),
    ],
)
async def test_ingest_text_document_e2e(
    client: AsyncClient,
    tmp_storage: Path,
    filename: str,
    upload_mime: str,
    expected_mime: str,
    raw_filename: str,
    content: str,
) -> None:
    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": (filename, content.encode(), upload_mime)},
    )
    assert response.status_code == 200

    job_id = response.json()["job_id"]
    job_response = await client.get(f"/v1/ingest/jobs/{job_id}")
    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "succeeded"

    document_id = job["document_id"]
    document_response = await client.get(f"/v1/documents/{document_id}")
    assert document_response.status_code == 200
    assert document_response.json()["mime_type"] == expected_mime

    chunks_response = await client.get(f"/v1/documents/{document_id}/chunks")
    assert chunks_response.status_code == 200
    chunks = chunks_response.json()
    assert len(chunks) >= 1
    assert content.strip() in "\n".join(chunk["text"] for chunk in chunks)
    assert all(chunk["page_no"] is None for chunk in chunks)

    artifacts_response = await client.get(f"/v1/documents/{document_id}/artifacts")
    assert artifacts_response.status_code == 200
    assert raw_filename in artifacts_response.json()["files"]

    bundle = tmp_storage / "artifacts" / document_id / "v1"
    assert (bundle / raw_filename).read_bytes() == content.encode()


@pytest.mark.asyncio
async def test_source_capabilities_include_text_formats(client: AsyncClient) -> None:
    response = await client.get("/v1/sources/capabilities")

    assert response.status_code == 200
    mime_types = response.json()["sources"][0]["mime_types"]
    assert "application/pdf" in mime_types
    assert "text/plain" in mime_types
    assert "text/markdown" in mime_types
