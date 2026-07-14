"""混合检索编排器：向量检索 + BM25 + 融合 + 重排。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.retrieval.keyword_retriever import get_keyword_retriever
from app.core.retrieval.reranker import get_reranker
from app.core.retrieval.vector_retriever import get_vector_retriever
from app.settings import get_settings


class HybridSearch:
    """混合检索门面，协调向量检索、关键词检索、融合和重排。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._vector = get_vector_retriever()
        self._keyword = get_keyword_retriever()
        self._reranker = get_reranker()

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

        tasks = []
        if enable_vector:
            tasks.append(self._vector.retrieve(query, vector_top_k, filters))
        if enable_keyword:
            tasks.append(self._keyword.retrieve(query, keyword_top_k, filters))

        if tasks:
            gathered: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[assignment]
            idx = 0
            if enable_vector:
                result: Any = gathered[idx]
                idx += 1
                if not isinstance(result, BaseException):
                    vector_results = result
            if enable_keyword:
                result = gathered[idx]
                if not isinstance(result, BaseException):
                    keyword_results = result

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

        if enable_rerank and merged:
            rerank_candidates = merged[: self._settings.rerank_top_k]
            merged = await self._reranker.rerank(query, rerank_candidates, top_k)

        results = _format_results(merged[:top_k])
        retrieval_time_ms = round((time.perf_counter() - t0) * 1000, 2)

        return {
            "results": results,
            "total": len(results),
            "fusion_method": fusion_method,
            "retrieval_time_ms": retrieval_time_ms,
            "components": components,
        }


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
            "text": item.get("text", ""),
            "score": item.get("score", 0),
            "vector_score": item.get("vector_score"),
            "keyword_score": item.get("keyword_score"),
            "rerank_score": item.get("rerank_score"),
            "source": {
                "document_id": item.get("doc_id") or item.get("document_id", ""),
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
