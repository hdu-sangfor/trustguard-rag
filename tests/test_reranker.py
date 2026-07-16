from __future__ import annotations

import pytest

from app.core.retrieval.reranker import RerankError, Reranker, normalize_rerank_provider
from app.settings import Settings


def _candidates() -> list[dict]:
    return [
        {"chunk_id": "a", "text": "第一段", "score": 0.9},
        {"chunk_id": "b", "text": "第二段", "score": 0.8},
        {"chunk_id": "c", "text": "第三段", "score": 0.7},
    ]


@pytest.mark.asyncio
async def test_api_reranker_calls_compatible_api(monkeypatch) -> None:
    calls = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.25},
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

    monkeypatch.setattr("app.core.retrieval.reranker.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        _env_file=None,
        rerank_provider="api",
        rerank_model="qwen3-rerank",
        rerank_base_url="https://workspace.example/compatible-api/v1/",
        rerank_api_key="secret",
        rerank_instruction="Retrieve semantically similar text.",
        rerank_api_timeout_seconds=12,
    )

    results = await Reranker(settings).rerank("查询", _candidates(), top_k=2)

    assert [item["chunk_id"] for item in results] == ["b", "a"]
    assert [item["rerank_score"] for item in results] == [0.95, 0.25]
    assert results[0]["score"] == 0.8
    assert calls == [
        (
            "https://workspace.example/compatible-api/v1/reranks",
            {
                "model": "qwen3-rerank",
                "query": "查询",
                "documents": ["第一段", "第二段", "第三段"],
                "top_n": 2,
                "instruct": "Retrieve semantically similar text.",
            },
            {"Authorization": "Bearer secret"},
            12.0,
        )
    ]


@pytest.mark.asyncio
async def test_api_reranker_accepts_dashscope_api_key(monkeypatch) -> None:
    captured_headers = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.6}]}

    class _AsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            captured_headers.update(headers)
            return _Response()

    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-secret")
    monkeypatch.setattr("app.core.retrieval.reranker.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        _env_file=None,
        rerank_provider="api",
        rerank_model="qwen3-rerank",
        rerank_base_url="https://workspace.example/compatible-api/v1",
    )

    results = await Reranker(settings).rerank("查询", _candidates(), top_k=1)

    assert results[0]["rerank_score"] == 0.6
    assert captured_headers == {"Authorization": "Bearer dashscope-secret"}


@pytest.mark.asyncio
async def test_api_reranker_raises_typed_error_when_not_configured() -> None:
    settings = Settings(_env_file=None, rerank_provider="api")

    with pytest.raises(RerankError, match="RAG_RERANK_BASE_URL is required"):
        await Reranker(settings).rerank("查询", _candidates(), top_k=2)


def test_default_reranker_does_not_require_optional_local_model() -> None:
    assert Settings(_env_file=None).rerank_provider == "none"


@pytest.mark.parametrize(
    ("configured", "normalized"),
    [
        ("local", "local"),
        ("bge", "local"),
        ("api", "api"),
        ("openai_compatible", "api"),
        ("bailian", "api"),
        ("dashscope", "api"),
        ("none", "none"),
    ],
)
def test_rerank_provider_aliases(configured: str, normalized: str) -> None:
    assert normalize_rerank_provider(configured) == normalized
