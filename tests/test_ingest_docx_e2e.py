"""DOCX ingest end-to-end test with a mocked MinerU service."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient

from app.core.ingest.extractors.file import MIME_ROUTER
from app.core.ingest.extractors.mineru import DOCX_MIME, MineruDocxExtractor
from app.settings import Settings


@pytest.mark.asyncio
async def test_ingest_docx_via_mineru_e2e(
    client: AsyncClient,
    tmp_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mineru_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": {
                    "word-sample": {"md_content": "# 应急响应\n\n发现安全事件后应当保留证据。"}
                }
            },
        )

    settings = Settings(
        mineru_base_url="http://mineru.test:8000",
        mineru_backend="pipeline",
    )
    monkeypatch.setitem(
        MIME_ROUTER,
        DOCX_MIME,
        MineruDocxExtractor(settings, transport=httpx.MockTransport(mineru_handler)),
    )

    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file"},
        files={"file": ("word-sample.docx", b"fake docx bytes", DOCX_MIME)},
    )
    assert response.status_code == 202

    job_id = response.json()["job_id"]
    job_response = await client.get(f"/v1/ingest/jobs/{job_id}")
    job = job_response.json()
    assert job["status"] == "succeeded"

    document_id = job["document_id"]
    document_response = await client.get(f"/v1/documents/{document_id}")
    document = document_response.json()
    assert document["mime_type"] == DOCX_MIME
    assert document["metadata"]["parser"] == "mineru"

    chunks_response = await client.get(f"/v1/documents/{document_id}/chunks")
    chunks = chunks_response.json()
    assert "保留证据" in "\n".join(chunk["text"] for chunk in chunks)

    artifacts_response = await client.get(f"/v1/documents/{document_id}/artifacts")
    assert "raw.docx" in artifacts_response.json()["files"]
    bundle = tmp_storage / "artifacts" / document_id / "v1"
    assert (bundle / "raw.docx").read_bytes() == b"fake docx bytes"


@pytest.mark.asyncio
async def test_source_capabilities_include_docx(client: AsyncClient) -> None:
    response = await client.get("/v1/sources/capabilities")

    assert response.status_code == 200
    mime_types = response.json()["sources"][0]["mime_types"]
    assert DOCX_MIME in mime_types
    parsers = response.json()["sources"][0]["parsers"]
    assert parsers[DOCX_MIME] == "mineru"
    assert parsers["application/pdf"] == "local"
    assert parsers["text/plain"] == "local"
