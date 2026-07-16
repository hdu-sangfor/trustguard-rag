"""混合检索编排器：向量检索 + BM25 + 融合 + 重排。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.retrieval.keyword_retriever import get_keyword_retriever
from app.core.retrieval.reranker import RerankError, get_reranker
from app.core.retrieval.vector_retriever import get_vector_retriever
from app.domain import EffectiveSearchMode, RetrievalComponent, SearchStatus
from app.settings import get_settings
from app.stores.document_store import get_document_store

logger = logging.getLogger(__name__)


class SearchUnavailableError(RuntimeError):
    """所有启用的召回后端均不可用。"""


class HybridSearch:
    """混合检索门面，协调向量检索、关键词检索、融合和重排。"""

    def __init__(self, document_store=None) -> None:
        self._settings = get_settings()
        self._vector = get_vector_retriever()
        self._keyword = get_keyword_retriever()
        self._reranker = get_reranker()
        self._documents = document_store or get_document_store()

    async def search(
        self,
        query: str,
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
        t0 = time.perf_counter()

        top_k = top_k or self._settings.search_top_k
        vector_top_k = vector_top_k or self._settings.search_vector_top_k
        keyword_top_k = keyword_top_k or self._settings.search_keyword_top_k
        fusion_method = fusion_method or self._settings.search_fusion_method
        vector_weight = vector_weight if vector_weight is not None else self._settings.search_vector_weight
        keyword_weight = keyword_weight if keyword_weight is not None else self._settings.search_keyword_weight

        vector_results: list[dict[str, Any]] = []
        keyword_results: list[dict[str, Any]] = []

        tasks: list[tuple[RetrievalComponent, Any]] = []
        if enable_vector:
            tasks.append(
                (
                    RetrievalComponent.VECTOR,
                    self._vector.retrieve(query, vector_top_k, filters),
                )
            )
        if enable_keyword:
            tasks.append(
                (
                    RetrievalComponent.KEYWORD,
                    self._keyword.retrieve(query, keyword_top_k, filters),
                )
            )

        if not tasks:
            raise ValueError("At least one retrieval backend must be enabled")

        degraded_components: list[RetrievalComponent] = []
        available_components: list[RetrievalComponent] = []
        if tasks:
            gathered: list[Any] = await asyncio.gather(
                *(task for _, task in tasks), return_exceptions=True
            )
            for (name, _), result in zip(tasks, gathered):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    degraded_components.append(name)
                    logger.warning(
                        "%s retrieval failed: %s",
                        name,
                        result,
                        exc_info=(type(result), result, result.__traceback__),
                    )
                elif name == RetrievalComponent.VECTOR:
                    available_components.append(name)
                    vector_results = result
                else:
                    available_components.append(name)
                    keyword_results = result

        if tasks and len(degraded_components) == len(tasks):
            raise SearchUnavailableError("All enabled retrieval backends are unavailable")

        if degraded_components and not vector_results and not keyword_results:
            raise SearchUnavailableError(
                "Retrieval is incomplete and no reliable result is available"
            )

        effective_mode = _effective_mode(available_components)

        components = {
            "vector": len(vector_results),
            "keyword": len(keyword_results),
        }

        merged = _merge_results(
            vector_results,
            keyword_results,
            fusion_method=fusion_method,
            vector_weight=vector_weight,
            keyword_weight=keyword_weight,
        )
        document_ids = list(
            {
                str(item.get("document_id"))
                for item in merged
                if item.get("document_id")
            }
        )
        ready_ids = await self._documents.ready_ids(document_ids)
        merged = [
            item
            for item in merged
            if str(item.get("document_id")) in ready_ids
        ]

        if enable_rerank and merged:
            rerank_candidates = merged[: self._settings.rerank_top_k]
            try:
                merged = await self._reranker.rerank(query, rerank_candidates, top_k)
            except RerankError as error:
                degraded_components.append(RetrievalComponent.RERANK)
                logger.warning(
                    "rerank failed: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
                merged = rerank_candidates[:top_k]

        results = _format_results(merged[:top_k])
        retrieval_time_ms = round((time.perf_counter() - t0) * 1000, 2)

        return {
            "search_status": (
                SearchStatus.DEGRADED if degraded_components else SearchStatus.OK
            ),
            "effective_mode": effective_mode,
            "results": results,
            "total": len(results),
            "fusion_method": fusion_method,
            "retrieval_time_ms": retrieval_time_ms,
            "components": components,
            "degraded_components": [item.value for item in degraded_components],
        }


def _effective_mode(
    available_components: list[RetrievalComponent],
) -> EffectiveSearchMode:
    available = set(available_components)
    if available == {RetrievalComponent.VECTOR, RetrievalComponent.KEYWORD}:
        return EffectiveSearchMode.HYBRID
    if RetrievalComponent.VECTOR in available:
        return EffectiveSearchMode.VECTOR_ONLY
    return EffectiveSearchMode.KEYWORD_ONLY


def _merge_results(
    vector_results: list[dict[str, Any]],
    keyword_results: list[dict[str, Any]],
    *,
    fusion_method: str,
    vector_weight: float,
    keyword_weight: float,
) -> list[dict[str, Any]]:
    if fusion_method == "rrf":
        return _rrf_fusion(vector_results, keyword_results)
    return _weighted_score_fusion(vector_results, keyword_results, vector_weight, keyword_weight)


def _rrf_fusion(
    vector_results: list[dict[str, Any]],
    keyword_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    settings = get_settings()
    k = settings.search_rrf_k
    merged: dict[str, dict[str, Any]] = {}

    def _add(source: list[dict[str, Any]], prefix: str):
        for rank, item in enumerate(source):
            cid = item.get("chunk_id") or item.get("_id") or str(rank)
            rrf_score = 1.0 / (k + rank + 1)
            if cid not in merged:
                merged[cid] = {**item}
            else:
                for key, value in item.items():
                    if merged[cid].get(key) is None and value is not None:
                        merged[cid][key] = value
            merged[cid].setdefault("rrf_score", 0.0)
            merged[cid]["rrf_score"] += rrf_score
            merged[cid][f"{prefix}_score"] = item.get("score")
            merged[cid][f"{prefix}_rank"] = rank + 1

    _add(vector_results, "vector")
    _add(keyword_results, "keyword")

    sorted_items = sorted(merged.values(), key=lambda x: x.get("rrf_score", 0), reverse=True)
    for item in sorted_items:
        item["score"] = item.get("rrf_score", 0)
    return sorted_items


def _weighted_score_fusion(
    vector_results: list[dict[str, Any]],
    keyword_results: list[dict[str, Any]],
    vector_weight: float,
    keyword_weight: float,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for item in vector_results:
        cid = item.get("chunk_id") or ""
        merged[cid] = {**item, "vector_score": item.get("score", 0), "keyword_score": None}
        merged[cid]["weighted_score"] = (item.get("score", 0) or 0) * vector_weight
        merged[cid]["score"] = merged[cid]["weighted_score"]

    for item in keyword_results:
        cid = item.get("chunk_id") or ""
        ks = item.get("score", 0) or 0
        if cid in merged:
            merged[cid]["keyword_score"] = ks
            merged[cid]["weighted_score"] = merged[cid].get("weighted_score", 0) + ks * keyword_weight
            merged[cid]["score"] = merged[cid]["weighted_score"]
        else:
            merged[cid] = {**item, "vector_score": None, "keyword_score": ks}
            merged[cid]["weighted_score"] = ks * keyword_weight
            merged[cid]["score"] = merged[cid]["weighted_score"]

    return sorted(merged.values(), key=lambda x: x.get("score", 0), reverse=True)


def _format_results(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": item.get("chunk_id") or item.get("_id", ""),
            "text": item.get("text") or "",
            "score": item.get("score", 0),
            "vector_score": item.get("vector_score"),
            "keyword_score": item.get("keyword_score"),
            "rerank_score": item.get("rerank_score"),
            "source": {
                "document_id": item.get("document_id", ""),
                "source_uri": item.get("source_uri", ""),
                "original_filename": item.get("original_filename"),
                "chunk_index": item.get("chunk_index", 0),
                "page_no": item.get("page_no"),
            },
            "metadata": item.get("metadata"),
        }
        for item in merged
    ]


def get_hybrid_search() -> HybridSearch:
    return HybridSearch()
