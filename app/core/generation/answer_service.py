"""检索、上下文构建、生成和引用校验的统一编排。"""

from __future__ import annotations

import time
from typing import Any

from app.core.generation.citation_validator import (
    parse_answer,
    render_declared_citations,
    validate_citations,
)
from app.core.generation.context_builder import ContextBuilder, Evidence
from app.core.generation.llm_client import LLMClient
from app.core.generation.llm_client import LLMResponseError
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
        knowledge_base_id: str,
        top_k: int | None = None,
        vector_top_k: int | None = None,
        keyword_top_k: int | None = None,
        max_chunks_per_document: int = 1,
        fusion_method: str | None = None,
        vector_weight: float | None = None,
        keyword_weight: float | None = None,
        enable_rerank: bool = True,
        enable_vector: bool = True,
        enable_keyword: bool = True,
        filters: dict[str, Any] | None = None,
        embedding_profile: str = "configured",
        enable_abstention: bool = True,
        allow_keyword_fallback: bool = False,
        min_vector_score: float | None = None,
        require_exact_entity_match: bool = True,
        component_max_retries: int | None = None,
        semantic_queries: list[str] | None = None,
        keyword_queries: list[str] | None = None,
        rerank_candidate_top_k: int | None = None,
        query_plan: dict[str, Any] | None = None,
        adjacent_chunk_radius: int = 0,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        search_result = await self._search.search(
            query=query,
            knowledge_base_id=knowledge_base_id,
            top_k=top_k,
            vector_top_k=vector_top_k,
            keyword_top_k=keyword_top_k,
            max_chunks_per_document=max_chunks_per_document,
            fusion_method=fusion_method,
            vector_weight=vector_weight,
            keyword_weight=keyword_weight,
            enable_rerank=enable_rerank,
            enable_vector=enable_vector,
            enable_keyword=enable_keyword,
            filters=filters,
            embedding_profile=embedding_profile,
            enable_abstention=enable_abstention,
            allow_keyword_fallback=allow_keyword_fallback,
            min_vector_score=min_vector_score,
            require_exact_entity_match=require_exact_entity_match,
            component_max_retries=component_max_retries,
            semantic_queries=semantic_queries,
            keyword_queries=keyword_queries,
            rerank_candidate_top_k=rerank_candidate_top_k,
            query_plan=query_plan,
            adjacent_chunk_radius=adjacent_chunk_radius,
        )
        context_max_chunks = (
            ((search_result.get("query_plan") or {}).get("effective_parameters") or {}).get(
                "context_max_chunks"
            )
        )
        bundle = self._context_builder.build(
            search_result["results"],
            max_chunks=context_max_chunks,
        )
        if not bundle.evidence:
            return self._build_response(
                query=query,
                knowledge_base_id=knowledge_base_id,
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
        messages = build_messages(query, bundle.context)
        completion = await self._llm.complete(messages)
        completions = [completion]
        try:
            parsed = render_declared_citations(
                parse_answer(completion.content),
                bundle.evidence,
            )
            cited_evidence = validate_citations(parsed, bundle.evidence)
        except LLMResponseError as error:
            repaired = await self._llm.complete(
                [
                    *messages,
                    {"role": "assistant", "content": completion.content},
                    {
                        "role": "user",
                        "content": (
                            "上一输出未通过格式或引用校验："
                            f"{error}。请仅修正 JSON、正文中的 [n] 和 citation_ids，"
                            "不得增加 EVIDENCE_JSON 中不存在的事实或编号。"
                        ),
                    },
                ]
            )
            completions.append(repaired)
            completion = repaired
            parsed = render_declared_citations(
                parse_answer(completion.content),
                bundle.evidence,
            )
            cited_evidence = validate_citations(parsed, bundle.evidence)
        generation_time_ms = round((time.perf_counter() - generation_started) * 1000, 2)
        answer_text = parsed.answer
        if parsed.status == AnswerStatus.INSUFFICIENT_EVIDENCE and not answer_text:
            answer_text = self._settings.answer_refusal_message

        usages = [item.usage for item in completions if item.usage is not None]
        usage = None
        if usages:
            usage = {
                "prompt_tokens": sum(item.prompt_tokens for item in usages),
                "completion_tokens": sum(item.completion_tokens for item in usages),
                "total_tokens": sum(item.total_tokens for item in usages),
            }
        return self._build_response(
            query=query,
            knowledge_base_id=knowledge_base_id,
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
        knowledge_base_id: str,
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
            "knowledge_base_id": knowledge_base_id,
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
            "abstained": search_result.get("abstained", False),
            "abstention_reason": search_result.get("abstention_reason"),
            "query_entities": search_result.get("query_entities", []),
            "component_attempts": search_result.get("component_attempts", {}),
            "recovered_components": search_result.get("recovered_components", []),
            "query_plan": search_result.get("query_plan"),
            "coverage_status": search_result.get(
                "coverage_status", "not_applicable"
            ),
            "coverage_warning": search_result.get("coverage_warning"),
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
