"""嵌入提供方测试。"""

from __future__ import annotations

import httpx
import pytest

from app.core.embedding import client as embedding_module
from app.core.embedding.client import EmbeddingClient, EmbeddingError
from app.settings import Settings, get_settings


def test_default_embedding_provider_keeps_lightweight_install_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAG_EMBEDDING_PROVIDER", raising=False)

    settings = Settings(_env_file=None)

    assert settings.embedding_provider == "pseudo"


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
async def test_api_embedding_batches_document_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    batch_sizes: list[int] = []

    class _Response:
        def __init__(self, size: int) -> None:
            self._size = size

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(index), 1.0]}
                    for index in range(self._size)
                ]
            }

    class _AsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            size = len(json["input"])
            batch_sizes.append(size)
            return _Response(size)

    monkeypatch.setattr("app.core.embedding.client.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        embedding_provider="api",
        embedding_base_url="http://embedding.local/v1",
        embedding_dim=2,
        embedding_batch_size=2,
    )

    vectors = await EmbeddingClient(settings).embed_texts(["a", "b", "c", "d", "e"])

    assert batch_sizes == [2, 2, 1]
    assert len(vectors) == 5


@pytest.mark.asyncio
async def test_api_embedding_adapts_to_provider_batch_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []

    class _Response:
        def __init__(self, size: int) -> None:
            self._size = size
            self.status_code = 400 if size > 2 else 200
            self.text = "batch size is invalid, it should not be larger than 2"

        def raise_for_status(self) -> None:
            if self.status_code == 200:
                return
            request = httpx.Request("POST", "http://embedding.local/v1/embeddings")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("batch too large", request=request, response=response)

        def json(self):
            return {
                "data": [
                    {"index": index, "embedding": [float(index), 1.0]}
                    for index in range(self._size)
                ]
            }

    class _AsyncClient:
        def __init__(self, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            size = len(json["input"])
            batch_sizes.append(size)
            return _Response(size)

    monkeypatch.setattr("app.core.embedding.client.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        embedding_provider="api",
        embedding_base_url="http://embedding.local/v1",
        embedding_dim=2,
        embedding_batch_size=4,
    )

    vectors = await EmbeddingClient(settings).embed_texts(["a", "b", "c"])

    assert batch_sizes == [3, 2, 1]
    assert len(vectors) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "retryable"), [(400, False), (429, True), (503, True)])
async def test_api_embedding_classifies_http_failures(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    retryable: bool,
) -> None:
    class _Response:
        text = "provider error"

        def __init__(self) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "http://embedding.local/v1/embeddings")
            response = httpx.Response(status_code, request=request)
            raise httpx.HTTPStatusError("provider error", request=request, response=response)

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
    settings = Settings(
        embedding_provider="api",
        embedding_base_url="http://embedding.local/v1",
        embedding_dim=2,
    )

    with pytest.raises(EmbeddingError) as exc_info:
        await EmbeddingClient(settings).embed_texts(["a"])

    assert exc_info.value.retryable is retryable


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


@pytest.mark.asyncio
async def test_local_embedding_runs_in_thread_and_validates_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    class _Provider:
        def encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
            calls.append((texts, is_query))
            return [[1.0, 0.0]]

    async def _to_thread(func, *args):
        return func(*args)

    monkeypatch.setattr(embedding_module, "_get_local_provider", lambda settings: _Provider())
    monkeypatch.setattr(embedding_module.asyncio, "to_thread", _to_thread)
    settings = Settings(embedding_provider="local", embedding_dim=3)

    with pytest.raises(EmbeddingError, match="dimension mismatch"):
        await EmbeddingClient(settings).embed_query("local query")

    assert calls == [(["local query"], True)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("embedding_batch_size", 32),
        ("embedding_normalize", False),
        ("embedding_query_instruction", "A different instruction"),
    ],
)
def test_local_provider_cache_tracks_behavior_settings(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    monkeypatch.setattr(embedding_module, "_LOCAL_PROVIDER", None)
    monkeypatch.setattr(embedding_module, "_LOCAL_PROVIDER_KEY", None)
    settings = Settings(embedding_provider="local")

    original = embedding_module._get_local_provider(settings)
    changed = settings.model_copy(update={field: value})

    assert embedding_module._get_local_provider(changed) is not original
