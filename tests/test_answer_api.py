from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.generation.llm_client import LLMConfigurationError, LLMTimeoutError


def _answer_result() -> dict:
    return {
        "query": "如何防御 SQL 注入？",
        "status": "answered",
        "answer": "应使用参数化查询。[1]",
        "citations": [
            {
                "citation_id": 1,
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "source_uri": "upload://security.pdf",
                "original_filename": "security.pdf",
                "chunk_index": 0,
                "page_no": 1,
                "excerpt": "参数化查询能够防御 SQL 注入。",
            }
        ],
        "search_status": "ok",
        "effective_mode": "hybrid",
        "degraded_components": [],
        "retrieved_count": 1,
        "context_chunk_count": 1,
        "context_token_count": 100,
        "retrieval_time_ms": 12.5,
        "generation_time_ms": 30.0,
        "total_time_ms": 42.5,
        "model": "qwen-plus",
        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    }


@pytest.mark.asyncio
async def test_answer_endpoint_returns_structured_answer(client, monkeypatch) -> None:
    service = SimpleNamespace(answer=AsyncMock(return_value=_answer_result()))
    monkeypatch.setattr("app.api.answer.get_answer_service", lambda: service)

    response = await client.post(
        "/v1/answer",
        json={
            "query": "如何防御 SQL 注入？",
            "enable_vector": True,
            "enable_keyword": True,
            "enable_rerank": True,
            "filters": {"source_uri": "upload://security.pdf"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["citations"][0]["chunk_id"] == "chunk-1"
    assert body["usage"]["total_tokens"] == 28
    assert service.answer.await_args.kwargs["filters"] == {"source_uri": "upload://security.pdf"}


@pytest.mark.asyncio
async def test_answer_endpoint_requires_retrieval_backend(client) -> None:
    response = await client.post(
        "/v1/answer",
        json={
            "query": "问题",
            "enable_vector": False,
            "enable_keyword": False,
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, expected_status",
    [
        (LLMConfigurationError("not configured"), 503),
        (LLMTimeoutError("timed out"), 504),
    ],
)
async def test_answer_endpoint_maps_llm_errors(
    client, monkeypatch, error: Exception, expected_status: int
) -> None:
    service = SimpleNamespace(answer=AsyncMock(side_effect=error))
    monkeypatch.setattr("app.api.answer.get_answer_service", lambda: service)

    response = await client.post(
        "/v1/answer",
        json={
            "query": "问题",
            "enable_vector": False,
            "enable_keyword": True,
            "enable_rerank": False,
        },
    )

    assert response.status_code == expected_status
