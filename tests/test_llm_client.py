from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.generation.llm_client import (
    LLMClient,
    LLMConfigurationError,
    LLMError,
    LLMResponseError,
    LLMTimeoutError,
    build_chat_completions_url,
)
from app.settings import Settings


class _Response:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.request = httpx.Request("POST", "https://llm.example/v1/chat/completions")

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(
                self.status_code,
                request=self.request,
                text="provider-secret-error-body",
            )
            raise httpx.HTTPStatusError("failed", request=self.request, response=response)


class _AsyncClient:
    response = _Response({})
    request: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _Response:
        type(self).request = {"url": url, **kwargs, "client": self.kwargs}
        return type(self).response


@pytest.mark.asyncio
async def test_llm_client_calls_openai_compatible_api(monkeypatch) -> None:
    _AsyncClient.response = _Response(
        {
            "model": "qwen-plus",
            "choices": [
                {
                    "message": {
                        "content": '{"status":"answered","answer":"答案。[1]","citation_ids":[1]}'
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
            },
        }
    )
    monkeypatch.setattr("app.core.generation.llm_client.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example/v1/",
        llm_api_key="secret",
        llm_model="qwen-plus",
    )

    result = await LLMClient(settings).complete([{"role": "user", "content": "问题"}])

    assert result.model == "qwen-plus"
    assert result.usage is not None and result.usage.total_tokens == 28
    assert _AsyncClient.request is not None
    assert _AsyncClient.request["url"] == "https://llm.example/v1/chat/completions"
    assert _AsyncClient.request["headers"] == {"Authorization": "Bearer secret"}
    assert _AsyncClient.request["json"]["response_format"] == {"type": "json_object"}
    assert _AsyncClient.request["json"]["stream"] is False


@pytest.mark.asyncio
async def test_llm_client_requires_configuration() -> None:
    with pytest.raises(LLMConfigurationError, match="disabled"):
        await LLMClient(Settings(_env_file=None, llm_provider="none")).complete([])


@pytest.mark.asyncio
async def test_llm_client_requires_model_name() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="api",
        llm_base_url="https://llm.example/v1",
        llm_api_key="secret",
        llm_model=" ",
    )
    with pytest.raises(LLMConfigurationError, match="RAG_LLM_MODEL"):
        await LLMClient(settings).complete([])


@pytest.mark.asyncio
async def test_llm_client_does_not_expose_provider_error_body(monkeypatch) -> None:
    _AsyncClient.response = _Response({}, status_code=500)
    monkeypatch.setattr("app.core.generation.llm_client.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        _env_file=None,
        llm_provider="api",
        llm_base_url="https://llm.example/v1",
        llm_api_key="secret",
    )

    with pytest.raises(LLMError) as exc_info:
        await LLMClient(settings).complete([])

    assert exc_info.value.retryable is True
    assert "provider-secret-error-body" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_llm_client_rejects_malformed_response(monkeypatch) -> None:
    _AsyncClient.response = _Response({"choices": []})
    monkeypatch.setattr("app.core.generation.llm_client.httpx.AsyncClient", _AsyncClient)
    settings = Settings(
        _env_file=None,
        llm_provider="api",
        llm_base_url="https://llm.example/v1",
        llm_api_key="secret",
    )

    with pytest.raises(LLMResponseError, match="invalid response"):
        await LLMClient(settings).complete([])


@pytest.mark.asyncio
async def test_llm_client_maps_timeout_without_leaking_request(monkeypatch) -> None:
    class _TimeoutClient(_AsyncClient):
        async def post(self, url: str, **kwargs: Any) -> _Response:
            raise httpx.ReadTimeout("upstream detail", request=httpx.Request("POST", url))

    monkeypatch.setattr("app.core.generation.llm_client.httpx.AsyncClient", _TimeoutClient)
    settings = Settings(
        _env_file=None,
        llm_provider="api",
        llm_base_url="https://llm.example/v1",
        llm_api_key="secret",
    )

    with pytest.raises(LLMTimeoutError, match="timed out") as exc_info:
        await LLMClient(settings).complete([])

    assert exc_info.value.retryable is True
    assert "upstream detail" not in str(exc_info.value)


def test_chat_completion_url_accepts_full_endpoint() -> None:
    assert build_chat_completions_url("https://llm.example/v1/chat/completions/") == (
        "https://llm.example/v1/chat/completions"
    )
