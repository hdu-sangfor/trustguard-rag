"""入库向量化模型选择与模型独立索引测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.embedding.profiles import (
    collection_name,
    get_embedding_profile,
    list_embedding_profiles,
    profile_settings,
)
from app.core.indexing.qdrant_indexer import QdrantIndexer
from app.settings import Settings
from app.stores.job_store import JobStore


def test_profiles_expose_local_and_bailian_models() -> None:
    settings = Settings(
        embedding_provider="pseudo",
        embedding_model="configured-model",
        embedding_dim=8,
        embedding_base_url=None,
    )

    profiles = list_embedding_profiles(settings)

    assert profiles[0].id == "configured"
    assert get_embedding_profile("bge-m3", settings).model == "BAAI/bge-m3"
    qwen37 = next(item for item in profiles if item.id == "qwen3.7-text-embedding")
    assert qwen37.dimension == 1024
    assert qwen37.retrieval_min_score == 0.60
    assert qwen37.available is False
    qwen37_native = next(
        item for item in profiles if item.id == "qwen3.7-text-embedding-2560"
    )
    assert qwen37_native.provider == "api"
    assert qwen37_native.api_driver == "bailian"
    assert qwen37_native.dimension == 2560
    assert qwen37_native.retrieval_min_score == 0.575
    text_v4_native = next(
        item for item in profiles if item.id == "text-embedding-v4-2048"
    )
    assert text_v4_native.dimension == 2048
    with pytest.raises(ValueError, match="RAG_EMBEDDING_BASE_URL"):
        get_embedding_profile("qwen3.7-text-embedding", settings)


def test_profile_uses_model_specific_collection() -> None:
    settings = Settings(qdrant_collection_prefix="test_", embedding_provider="pseudo")
    profile = get_embedding_profile("bge-m3", settings)
    selected = profile_settings(profile, settings)

    assert selected.embedding_model == "BAAI/bge-m3"
    assert selected.embedding_dim == 1024
    assert collection_name(profile, settings) == "test_chunks__bge-m3"
    assert QdrantIndexer(selected, profile=profile).collection_name == "test_chunks__bge-m3"


@pytest.mark.asyncio
async def test_upload_persists_selected_embedding_profile(client, monkeypatch) -> None:
    dispatch = AsyncMock()
    monkeypatch.setattr("app.api.ingest.dispatch_eager", dispatch)

    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file", "embedding_profile": "bge-m3"},
        files={"file": ("profile.txt", b"profile selection", "text/plain")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["embedding_profile"] == "bge-m3"
    assert body["embedding_model"] == "BAAI/bge-m3"
    job = await JobStore().get(body["job_id"])
    assert job is not None
    assert job.options_json["embedding_provider"] == "local"
    assert job.options_json["embedding_dim"] == 1024
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_rejects_unknown_embedding_profile(client) -> None:
    response = await client.post(
        "/v1/ingest/jobs",
        data={"source_type": "file", "embedding_profile": "not-allowed"},
        files={"file": ("profile.txt", b"profile selection", "text/plain")},
    )

    assert response.status_code == 400
    assert "Unsupported embedding profile" in response.json()["detail"]
