from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.generation.answer_service import AnswerService
from app.core.generation.context_builder import ContextBuilder
from app.core.generation.llm_client import LLMCompletion, LLMResponseError, LLMUsage
from app.domain import AnswerStatus, EffectiveSearchMode, SearchStatus
from app.settings import Settings


class _CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


def _search_result(results: list[dict]) -> dict:
    return {
        "search_status": SearchStatus.OK,
        "effective_mode": EffectiveSearchMode.HYBRID,
        "results": results,
        "total": len(results),
        "fusion_method": "rrf",
        "retrieval_time_ms": 12.5,
        "components": {"vector": len(results), "keyword": len(results)},
        "degraded_components": [],
    }


def _result() -> dict:
    return {
        "chunk_id": "chunk-1",
        "text": "参数化查询能够防御 SQL 注入。",
        "score": 1.0,
        "source": {
            "document_id": "doc-1",
            "source_uri": "upload://security.pdf",
            "original_filename": "security.pdf",
            "chunk_index": 2,
            "page_no": 3,
        },
    }


def _service(search_result: dict, completion: LLMCompletion | None):
    settings = Settings(
        _env_file=None,
        answer_context_max_tokens=2000,
        answer_max_context_chunks=5,
    )
    search = SimpleNamespace(search=AsyncMock(return_value=search_result))
    llm = SimpleNamespace(complete=AsyncMock(return_value=completion))
    builder = ContextBuilder(settings, token_counter=_CharacterCounter())
    return AnswerService(settings, search, builder, llm), search, llm


@pytest.mark.asyncio
async def test_answer_service_returns_grounded_answer() -> None:
    completion = LLMCompletion(
        content=('{"status":"answered","answer":"应使用参数化查询。[1]","citation_ids":[1]}'),
        model="qwen-plus",
        usage=LLMUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28),
    )
    service, search, llm = _service(_search_result([_result()]), completion)

    result = await service.answer("如何防御 SQL 注入？")

    assert result["status"] == AnswerStatus.ANSWERED
    assert result["citations"][0]["page_no"] == 3
    assert result["citations"][0]["chunk_id"] == "chunk-1"
    assert result["usage"]["total_tokens"] == 28
    assert result["context_chunk_count"] == 1
    search.search.assert_awaited_once()
    llm.complete.assert_awaited_once()
    messages = llm.complete.await_args.args[0]
    assert messages[0]["role"] == "system"
    assert "不可信资料" in messages[0]["content"]
    assert "EVIDENCE_JSON" in messages[1]["content"]


@pytest.mark.asyncio
async def test_answer_service_skips_llm_when_retrieval_is_empty() -> None:
    service, _, llm = _service(_search_result([]), None)

    result = await service.answer("知识库里没有的问题")

    assert result["status"] == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result["citations"] == []
    assert result["generation_time_ms"] == 0
    llm.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_service_preserves_model_refusal() -> None:
    completion = LLMCompletion(
        content=(
            '{"status":"insufficient_evidence","answer":"提供的资料无法确定答案。",'
            '"citation_ids":[]}'
        ),
        model="qwen-plus",
    )
    service, _, _ = _service(_search_result([_result()]), completion)

    result = await service.answer("无法回答的问题")

    assert result["status"] == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result["answer"] == "提供的资料无法确定答案。"
    assert result["citations"] == []


@pytest.mark.asyncio
async def test_answer_service_uses_default_refusal_when_model_answer_is_empty() -> None:
    completion = LLMCompletion(
        content=('{"status":"insufficient_evidence","answer":"","citation_ids":[]}'),
        model="qwen-plus",
    )
    service, _, _ = _service(_search_result([_result()]), completion)

    result = await service.answer("无法回答的问题")

    assert result["status"] == AnswerStatus.INSUFFICIENT_EVIDENCE
    assert result["answer"] == "当前知识库中没有足够证据回答该问题。"


@pytest.mark.asyncio
async def test_answer_service_rejects_unknown_citation() -> None:
    completion = LLMCompletion(
        content=('{"status":"answered","answer":"编造的引用。[9]","citation_ids":[9]}'),
        model="qwen-plus",
    )
    service, _, _ = _service(_search_result([_result()]), completion)

    with pytest.raises(LLMResponseError, match="was not provided"):
        await service.answer("问题")
