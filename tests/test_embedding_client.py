"""Embedding provider tests."""
from __future__ import annotations

import pytest

from app.core.embedding.client import EmbeddingClient, EmbeddingError
from app.settings import get_settings


@pytest.mark.asyncio
async def test_pseudo_embedding_returns_configured_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "pseudo")
    monkeypatch.setenv("RAG_EMBEDDING_DIM", "8")
    get_settings.cache_clear()

    vectors = await EmbeddingClient().embed_texts(["alpha", "beta"])

    assert len(vectors) == 2
    assert all(len(v) == 8 for v in vectors)
    assert vectors[0] == await EmbeddingClient().embed_query("alpha")


@pytest.mark.asyncio
async def test_api_embedding_provider_sends_openai_compatible_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            }

    class _AsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            calls.append((url, json, headers, self.timeout))
            return _Response()

    monkeypatch.setattr("app.core.embedding.client.httpx.AsyncClient", _AsyncClient)
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "api")
    monkeypatch.setenv("RAG_EMBEDDING_BASE_URL", "http://embedding.local/v1")
    monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "secret")
    monkeypatch.setenv("RAG_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    monkeypatch.setenv("RAG_EMBEDDING_DIM", "2")
    monkeypatch.setenv("RAG_EMBEDDING_API_TIMEOUT_SECONDS", "12")
    get_settings.cache_clear()

    vectors = await EmbeddingClient().embed_texts(["a", "b"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert calls[0][0] == "http://embedding.local/v1/embeddings"
    assert calls[0][1]["model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert calls[0][1]["input"] == ["a", "b"]
    assert calls[0][2]["Authorization"] == "Bearer secret"
    assert calls[0][3] == 12.0


@pytest.mark.asyncio
async def test_embedding_dimension_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}

    class _AsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            return _Response()

    monkeypatch.setattr("app.core.embedding.client.httpx.AsyncClient", _AsyncClient)
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "api")
    monkeypatch.setenv("RAG_EMBEDDING_BASE_URL", "http://embedding.local/v1")
    monkeypatch.setenv("RAG_EMBEDDING_DIM", "3")
    get_settings.cache_clear()

    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        await EmbeddingClient().embed_texts(["a"])
