"""知识库模型绑定与检索范围隔离测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import DocumentStatus
from app.core.embedding.profiles import get_embedding_profile
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore
from app.stores.knowledge_base_migration import migrate_legacy_knowledge_bases
from app.stores.knowledge_base_store import KnowledgeBaseStore
from app.stores.models import DocumentRow


@pytest.mark.asyncio
async def test_default_and_custom_knowledge_bases_are_listed(client) -> None:
    initial = await client.get("/v1/knowledge-bases")
    assert initial.status_code == 200
    assert initial.json()["items"][0]["is_default"] is True

    created = await client.post(
        "/v1/knowledge-bases",
        json={"name": "安全运营", "embedding_profile": "bge-m3"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["embedding_profile"] == "bge-m3"
    assert body["embedding_model"] == "BAAI/bge-m3"
    assert body["embedding_dim"] == 1024

    duplicate = await client.post(
        "/v1/knowledge-bases",
        json={"name": "安全运营", "embedding_profile": "qwen3-embedding-0.6b"},
    )
    assert duplicate.status_code == 409


@pytest.mark.asyncio
async def test_upload_uses_model_fixed_by_knowledge_base(client, monkeypatch) -> None:
    created = await client.post(
        "/v1/knowledge-bases",
        json={"name": "终端安全", "embedding_profile": "bge-m3"},
    )
    knowledge_base_id = created.json()["id"]

    async def dispatch(_event) -> None:
        return None

    monkeypatch.setattr("app.api.ingest.dispatch_eager", dispatch)
    uploaded = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file", "knowledge_base_id": knowledge_base_id},
        files={"file": ("guide.txt", b"endpoint security", "text/plain")},
    )
    assert uploaded.status_code == 202
    body = uploaded.json()
    assert body["knowledge_base_id"] == knowledge_base_id
    assert body["embedding_profile"] == "bge-m3"

    mismatch = await client.post(
        "/v1/ingest/jobs",
        data={
            "source_type": "file",
            "knowledge_base_id": knowledge_base_id,
            "embedding_profile": "qwen3-embedding-0.6b",
        },
        files={"file": ("other.txt", b"other", "text/plain")},
    )
    assert mismatch.status_code == 400


@pytest.mark.asyncio
async def test_search_forces_knowledge_base_filter(client, monkeypatch) -> None:
    created = await client.post(
        "/v1/knowledge-bases",
        json={"name": "云安全", "embedding_profile": "qwen3-embedding-0.6b"},
    )
    knowledge_base_id = created.json()["id"]
    captured = {}

    class _Search:
        async def search(self, **kwargs):
            captured.update(kwargs)
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
        json={
            "query": "如何处理云密钥泄露",
            "knowledge_base_id": knowledge_base_id,
            "enable_vector": False,
            "enable_keyword": True,
            "filters": {"source_uri": "upload://guide.pdf"},
        },
    )
    assert response.status_code == 200
    assert response.json()["knowledge_base_id"] == knowledge_base_id
    assert captured["filters"] == {
        "source_uri": "upload://guide.pdf",
        "knowledge_base_id": knowledge_base_id,
    }
    assert captured["knowledge_base_id"] == knowledge_base_id
    assert captured["embedding_profile"] == "qwen3-embedding-0.6b"


@pytest.mark.asyncio
async def test_search_requires_explicit_knowledge_base(client) -> None:
    missing = await client.post("/v1/search", json={"query": "不能搜索全部知识库"})
    assert missing.status_code == 422

    blank = await client.post(
        "/v1/search",
        json={"query": "不能搜索空范围", "knowledge_base_id": "   "},
    )
    assert blank.status_code == 422

    legacy = await client.post(
        "/v1/search",
        json={"query": "不能按模型代替知识库", "embedding_profile": "bge-m3"},
    )
    assert legacy.status_code == 422


@pytest.mark.asyncio
async def test_core_search_overrides_conflicting_scope_filter(
    client, monkeypatch
) -> None:
    created = await client.post(
        "/v1/knowledge-bases",
        json={"name": "隔离验证", "embedding_profile": "qwen3-embedding-0.6b"},
    )
    knowledge_base_id = created.json()["id"]
    captured = {}

    class _Retriever:
        async def retrieve(self, _query, _top_k, filters):
            captured.update(filters)
            return []

    from app.core.retrieval.search import HybridSearch

    engine = HybridSearch()
    engine._keyword = _Retriever()
    result = await engine.search(
        "隔离测试",
        knowledge_base_id=knowledge_base_id,
        enable_vector=False,
        enable_keyword=True,
        enable_rerank=False,
        filters={"knowledge_base_id": "another-knowledge-base"},
    )

    assert result["total"] == 0
    assert captured["knowledge_base_id"] == knowledge_base_id


@pytest.mark.asyncio
async def test_legacy_documents_are_grouped_by_embedding_profile(test_engine) -> None:
    document_id = str(uuid4())
    async with AsyncSession(test_engine) as session:
        document = DocumentRow(
            id=document_id,
            knowledge_base_id=None,
            source_type="file",
            source_uri="upload://legacy.txt",
            content_hash="a" * 64,
            status=DocumentStatus.READY,
            original_filename="legacy.txt",
        )
        session.add(document)
        await session.commit()
    await ChunkStore().create_many(
        [
                {
                    "document_id": document_id,
                "chunk_index": 0,
                "text": "legacy content",
                "metadata": {"embedding_profile": "bge-m3"},
            }
        ]
    )

    assert await migrate_legacy_knowledge_bases() == 1
    migrated = await DocumentStore().get(document_id)
    assert migrated is not None and migrated.knowledge_base_id
    knowledge_base = await KnowledgeBaseStore().get(migrated.knowledge_base_id)
    assert knowledge_base is not None
    assert knowledge_base.embedding_profile == "bge-m3"


@pytest.mark.asyncio
async def test_active_ingest_job_blocks_knowledge_base_deletion(test_engine) -> None:
    store = KnowledgeBaseStore()
    knowledge_base = await store.create(
        name="等待入库的知识库",
        profile=get_embedding_profile("configured"),
    )
    await JobStore().create_ingest_command(
        job_id=str(uuid4()),
        source_type="file",
        source="queued.pdf",
        knowledge_base_id=knowledge_base.id,
    )

    with pytest.raises(ValueError, match="active ingest jobs"):
        await store.delete(knowledge_base.id)

    assert await store.get(knowledge_base.id) is not None


@pytest.mark.asyncio
async def test_ingest_command_rejects_deleted_knowledge_base(test_engine) -> None:
    with pytest.raises(LookupError, match="Knowledge base not found"):
        await JobStore().create_ingest_command(
            job_id=str(uuid4()),
            source_type="file",
            source="orphan.pdf",
            knowledge_base_id=str(uuid4()),
        )
