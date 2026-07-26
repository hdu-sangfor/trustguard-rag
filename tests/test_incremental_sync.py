"""增量入库 Sync Bridge 单元/API 测试。"""
from __future__ import annotations

import base64

import pytest
from httpx import AsyncClient

from app.stores.cursor_store import CursorStore
from pdf_fixtures import make_pdf_bytes


@pytest.mark.asyncio
async def test_cursor_store_get_set(test_engine) -> None:
    store = CursorStore()
    assert await store.get("missing") is None
    row = await store.set("demo-sync", "20260101T000000Z:abc")
    assert row.cursor_key == "demo-sync"
    assert await store.get("demo-sync") == "20260101T000000Z:abc"
    await store.set("demo-sync", "20260101T010000Z:def")
    assert await store.get("demo-sync") == "20260101T010000Z:def"


@pytest.mark.asyncio
async def test_stable_uri_auto_keep_new(client: AsyncClient) -> None:
    uri = "crawler://unit/stable-a.md"
    pdf1 = make_pdf_bytes(["Stable URI version one"])
    pdf2 = make_pdf_bytes(["Stable URI version two different"])

    r1 = await client.post(
        "/v1/ingest/jobs",
        data={
            "source_type": "file",
            "source_uri": uri,
            "conflict_policy": "keep_new",
        },
        files={"file": ("stable-a.pdf", pdf1, "application/pdf")},
    )
    assert r1.status_code == 202
    job1 = (await client.get(f"/v1/ingest/jobs/{r1.json()['job_id']}")).json()
    assert job1["status"] == "succeeded"
    old_id = job1["document_id"]

    r2 = await client.post(
        "/v1/ingest/jobs",
        data={
            "source_type": "file",
            "source_uri": uri,
            "conflict_policy": "keep_new",
        },
        files={"file": ("stable-a.pdf", pdf2, "application/pdf")},
    )
    assert r2.status_code == 202
    job2 = (await client.get(f"/v1/ingest/jobs/{r2.json()['job_id']}")).json()
    assert job2["status"] == "succeeded"
    assert job2["document_id"] != old_id

    old_doc = (await client.get(f"/v1/documents/{old_id}")).json()
    assert old_doc["status"] == "superseded"
    new_doc = (await client.get(f"/v1/documents/{job2['document_id']}")).json()
    assert new_doc["status"] == "ready"
    assert new_doc["source_uri"] == uri


@pytest.mark.asyncio
async def test_sync_items_skip_update_cleanup(client: AsyncClient) -> None:
    prefix = "crawler://unit/sync/"
    uri_a = f"{prefix}a.md"
    uri_b = f"{prefix}b.md"
    content_a = b"# Doc A\nhello sync"
    content_b = b"# Doc B\nworld sync"
    content_a2 = b"# Doc A\nhello sync changed"

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    first = await client.post(
        "/v1/ingest/sync",
        json={
            "conflict_policy": "keep_new",
            "cleanup": "none",
            "cursor_key": "unit-sync",
            "wait": True,
            "items": [
                {
                    "source_uri": uri_a,
                    "filename": "a.md",
                    "content_b64": b64(content_a),
                    "mime": "text/markdown",
                },
                {
                    "source_uri": uri_b,
                    "filename": "b.md",
                    "content_b64": b64(content_b),
                    "mime": "text/markdown",
                },
            ],
        },
    )
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1["added"] == 2
    assert body1["skipped"] == 0
    assert body1["failed"] == 0
    assert body1["cursor_value"]

    cursor = (await client.get("/v1/ingest/cursors/unit-sync")).json()
    assert cursor["cursor_value"] == body1["cursor_value"]

    second = await client.post(
        "/v1/ingest/sync",
        json={
            "conflict_policy": "keep_new",
            "cleanup": "none",
            "cursor_key": "unit-sync",
            "wait": True,
            "items": [
                {
                    "source_uri": uri_a,
                    "filename": "a.md",
                    "content_b64": b64(content_a),
                    "mime": "text/markdown",
                },
                {
                    "source_uri": uri_b,
                    "filename": "b.md",
                    "content_b64": b64(content_b),
                    "mime": "text/markdown",
                },
            ],
        },
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["skipped"] == 2
    assert body2["added"] == 0
    assert body2["updated"] == 0

    third = await client.post(
        "/v1/ingest/sync",
        json={
            "conflict_policy": "keep_new",
            "cleanup": "full",
            "source_uri_prefix": prefix,
            "cursor_key": "unit-sync",
            "wait": True,
            "items": [
                {
                    "source_uri": uri_a,
                    "filename": "a.md",
                    "content_b64": b64(content_a2),
                    "mime": "text/markdown",
                },
            ],
        },
    )
    assert third.status_code == 200, third.text
    body3 = third.json()
    assert body3["updated"] == 1
    assert body3["deleted"] == 1
    assert body3["failed"] == 0

    caps = (await client.get("/v1/sources/capabilities")).json()
    assert "sync" in caps
    assert caps["sync"]["endpoint"] == "/v1/ingest/sync"
