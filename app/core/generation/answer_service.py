"""检索、上下文构建、生成和引用校验的统一编排。"""

from __future__ import annotations

import time
from typing import Any

from app.core.generation.citation_validator import parse_answer, validate_citations
from app.core.generation.context_builder import ContextBuilder, Evidence
from app.core.generation.llm_client import LLMClient
from app.core.generation.prompts import build_messages
from app.core.retrieval.search import HybridSearch, get_hybrid_search
from app.domain import AnswerStatus
from app.settings import Settings, get_settings


class AnswerService:
    """在保留检索诊断信息的同时生成有依据的答案。"""

    def __init__(
        self,
        settings: Settings | None = None,
        search_engine: HybridSearch | None = None,
        context_builder: ContextBuilder | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._search = search_engine or get_hybrid_search()
        self._context_builder = context_builder or ContextBuilder(self._settings)
        self._llm = llm_client or LLMClient(self._settings)

    async def answer(
        self,
        query: str,
        *,
        top_k: int | None = None,
        vector_top_k: int | None = None,
        keyword_top_k: int | None = None,
        fusion_method: str | None = None,
        vector_weight: float | None = None,
        keyword_weight: float | None = None,
        enable_rerank: bool = True,
        enable_vector: bool = True,
        enable_keyword: bool = True,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        search_result = await self._search.search(
            query=query,
            top_k=top_k,
            vector_top_k=vector_top_k,
            keyword_top_k=keyword_top_k,
            fusion_method=fusion_method,
            vector_weight=vector_weight,
            keyword_weight=keyword_weight,
            enable_rerank=enable_rerank,
            enable_vector=enable_vector,
            enable_keyword=enable_keyword,
            filters=filters,
        )
        bundle = self._context_builder.build(search_result["results"])
        if not bundle.evidence:
            return self._build_response(
                query=query,
                search_result=search_result,
                status=AnswerStatus.INSUFFICIENT_EVIDENCE,
                answer=self._settings.answer_refusal_message,
                citations=[],
                context_chunk_count=0,
                context_token_count=bundle.token_count,
                generation_time_ms=0.0,
                total_started=started,
            )

        generation_started = time.perf_counter()
        completion = await self._llm.complete(build_messages(query, bundle.context))
        generation_time_ms = round((time.perf_counter() - generation_started) * 1000, 2)
        parsed = parse_answer(completion.content)
        cited_evidence = validate_citations(parsed, bundle.evidence)
        answer_text = parsed.answer
        if parsed.status == AnswerStatus.INSUFFICIENT_EVIDENCE and not answer_text:
            answer_text = self._settings.answer_refusal_message

        usage = None
        if completion.usage is not None:
            usage = {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            }
        return self._build_response(
            query=query,
            search_result=search_result,
            status=parsed.status,
            answer=answer_text,
            citations=cited_evidence,
            context_chunk_count=len(bundle.evidence),
            context_token_count=bundle.token_count,
            generation_time_ms=generation_time_ms,
            total_started=started,
            model=completion.model,
            usage=usage,
        )

    @staticmethod
    def _build_response(
        *,
        query: str,
        search_result: dict[str, Any],
        status: AnswerStatus,
        answer: str,
        citations: list[Evidence],
        context_chunk_count: int,
        context_token_count: int,
        generation_time_ms: float,
        total_started: float,
        model: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "status": status,
            "answer": answer,
            "citations": [
                {
                    "citation_id": item.citation_id,
                    "chunk_id": item.chunk_id,
                    "document_id": item.document_id,
                    "source_uri": item.source_uri,
                    "original_filename": item.original_filename,
                    "chunk_index": item.chunk_index,
                    "page_no": item.page_no,
                    "excerpt": item.text,
                }
                for item in citations
            ],
            "search_status": search_result["search_status"],
            "effective_mode": search_result["effective_mode"],
            "degraded_components": search_result["degraded_components"],
            "retrieved_count": search_result["total"],
            "context_chunk_count": context_chunk_count,
            "context_token_count": context_token_count,
            "retrieval_time_ms": search_result["retrieval_time_ms"],
            "generation_time_ms": generation_time_ms,
            "total_time_ms": round((time.perf_counter() - total_started) * 1000, 2),
            "model": model,
            "usage": usage,
        }


def get_answer_service() -> AnswerService:
    return AnswerService()
