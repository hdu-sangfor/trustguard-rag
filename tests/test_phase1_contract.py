"""Phase 1：内容版本、稳定响应、内部鉴权和精确 Chunk 读取。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import inspect, text

from app.core.embedding.profiles import get_embedding_profile
from app.domain import DocumentStatus, IngestJobStatus
from app.settings import get_settings
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobLease, JobStore
from app.stores.knowledge_base_migration import ensure_knowledge_base_schema
from app.stores.knowledge_base_store import KnowledgeBaseStore


@pytest.mark.asyncio
async def test_content_revision_migration_is_idempotent(test_engine) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE knowledge_bases DROP COLUMN content_revision"))

    await ensure_knowledge_base_schema()
    await ensure_knowledge_base_schema()

    async with test_engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                item["name"] for item in inspect(sync_conn).get_columns("knowledge_bases")
            }
        )
        assert "content_revision" in columns


@pytest.mark.asyncio
async def test_content_revision_tracks_searchable_set_changes(test_engine) -> None:
    knowledge_bases = KnowledgeBaseStore()
    knowledge_base = await knowledge_bases.create(
        name="版本测试知识库",
        profile=get_embedding_profile("configured"),
    )
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://revision.txt",
        content_hash="a" * 64,
        status=DocumentStatus.STAGING,
        original_filename="revision.txt",
        knowledge_base_id=knowledge_base.id,
    )

    assert (await knowledge_bases.get(knowledge_base.id)).content_revision == 0

    await DocumentStore().update_status(document.id, DocumentStatus.INDEXING)
    await DocumentStore().update_status(document.id, DocumentStatus.FAILED)
    assert (await knowledge_bases.get(knowledge_base.id)).content_revision == 0

    await DocumentStore().update_status(document.id, DocumentStatus.READY)
    assert (await knowledge_bases.get(knowledge_base.id)).content_revision == 1

    await DocumentStore().update_status(document.id, DocumentStatus.READY)
    assert (await knowledge_bases.get(knowledge_base.id)).content_revision == 1

    await DocumentStore().request_delete(document.id)
    assert (await knowledge_bases.get(knowledge_base.id)).content_revision == 2


@pytest.mark.asyncio
async def test_atomic_job_publication_increments_revision(test_engine) -> None:
    knowledge_base = await KnowledgeBaseStore().create(
        name="原子发布版本测试",
        profile=get_embedding_profile("configured"),
    )
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://publish.txt",
        content_hash="c" * 64,
        status=DocumentStatus.INDEXING,
        knowledge_base_id=knowledge_base.id,
    )
    job = await JobStore().create(
        source_type="file",
        source="publish.txt",
        knowledge_base_id=knowledge_base.id,
    )
    lease = await JobStore().claim(
        job.id,
        allowed_statuses=(IngestJobStatus.QUEUED,),
    )
    assert isinstance(lease, JobLease)

    await JobStore().publish_document(job.id, document.id, lease_token=lease.token)

    assert (await DocumentStore().get(document.id)).status == DocumentStatus.READY
    assert (await KnowledgeBaseStore().get(knowledge_base.id)).content_revision == 1


@pytest.mark.asyncio
async def test_search_response_exposes_stable_schema_revision_and_request_id(
    client, monkeypatch
) -> None:
    knowledge_base = await KnowledgeBaseStore().create(
        name="稳定搜索契约",
        profile=get_embedding_profile("configured"),
    )

    class _Search:
        async def search(self, **_kwargs):
            return {
                "search_status": "ok",
                "effective_mode": "keyword_only",
                "results": [],
                "total": 0,
                "fusion_method": "rrf",
                "retrieval_time_ms": 1.0,
                "components": {"vector": 0, "keyword": 0},
                "degraded_components": [],
            }

    monkeypatch.setattr(
        "app.application.knowledge.get_hybrid_search",
        lambda: _Search(),
    )
    response = await client.post(
        "/v1/search",
        headers={"X-Request-ID": "req-phase1-search"},
        json={
            "query": "稳定响应",
            "knowledge_base_id": knowledge_base.id,
            "enable_vector": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert response.headers["X-Request-ID"] == "req-phase1-search"
    assert body["schema_version"] == "trustguard-search-v1"
    assert body["request_id"] == "req-phase1-search"
    assert body["content_revision"] == 0
    assert body["query_plan"] == {"intent": "focused", "source": "explicit"}
    assert body["coverage"] == {"status": "not_applicable", "warning": None}


@pytest.mark.asyncio
async def test_validation_error_uses_stable_envelope_and_keeps_detail(client) -> None:
    response = await client.post(
        "/v1/search",
        headers={"X-Request-ID": "req-phase1-error"},
        json={"query": "缺少知识库"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["schema_version"] == "trustguard-error-v1"
    assert body["request_id"] == "req-phase1-error"
    assert body["code"] == "INVALID_ARGUMENT"
    assert body["retryable"] is False
    assert isinstance(body["detail"], list)


@pytest.mark.asyncio
async def test_internal_chunk_read_requires_auth_and_enforces_scope(client, monkeypatch) -> None:
    first = await KnowledgeBaseStore().create(
        name="内部读取知识库 A",
        profile=get_embedding_profile("configured"),
    )
    second = await KnowledgeBaseStore().create(
        name="内部读取知识库 B",
        profile=get_embedding_profile("configured"),
    )
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://internal.txt",
        content_hash="b" * 64,
        status=DocumentStatus.READY,
        title="内部读取测试",
        original_filename="internal.txt",
        knowledge_base_id=first.id,
    )
    chunk_id = str(uuid4())
    await ChunkStore().create_many(
        [
            {
                "id": chunk_id,
                "document_id": document.id,
                "chunk_index": 0,
                "text": "只能由匹配知识库和服务身份读取",
                "metadata": {"content_type": "security_guide"},
            }
        ]
    )
    path = f"/v1/internal/knowledge-bases/{first.id}/chunks/{chunk_id}"

    get_settings.cache_clear()
    unconfigured = await client.get(path)
    assert unconfigured.status_code == 503
    assert unconfigured.json()["code"] == "RAG_UNAVAILABLE"

    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "phase1-secret")
    get_settings.cache_clear()
    unauthorized = await client.get(path, headers={"Authorization": "Bearer wrong"})
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"

    authorized = await client.get(
        path,
        headers={
            "Authorization": "Bearer phase1-secret",
            "X-Request-ID": "req-phase1-resource",
        },
    )
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["schema_version"] == "trustguard-internal-chunk-v1"
    assert body["request_id"] == "req-phase1-resource"
    assert body["knowledge_base_id"] == first.id
    assert body["chunk_id"] == chunk_id
    assert body["text"] == "只能由匹配知识库和服务身份读取"

    cross_scope = await client.get(
        f"/v1/internal/knowledge-bases/{second.id}/chunks/{chunk_id}",
        headers={"Authorization": "Bearer phase1-secret"},
    )
    assert cross_scope.status_code == 404
    assert cross_scope.json()["detail"] == "Chunk not found"

    await DocumentStore().update_status(document.id, DocumentStatus.FAILED)
    unpublished = await client.get(
        path,
        headers={"Authorization": "Bearer phase1-secret"},
    )
    assert unpublished.status_code == 404

    get_settings.cache_clear()
