"""混合检索编排器：向量检索 + BM25 + 融合 + 重排。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.retrieval.filters import ENTITY_IDS_ANY_FILTER
from app.core.retrieval.keyword_retriever import get_keyword_retriever
from app.core.retrieval.reranker import RerankError, get_reranker
from app.core.retrieval.security_entities import (
    build_security_entity_fields,
    exact_entity_match_priority,
    extract_security_entity_ids,
)
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
        min_vector_score: float | None = None,
        require_exact_entity_match: bool = True,
        component_max_retries: int | None = None,
    ) -> dict[str, Any]:
        t0 = time.perf_counter()
        knowledge_base_id = knowledge_base_id.strip()
        if not knowledge_base_id:
            raise ValueError("knowledge_base_id is required")
        if not 1 <= max_chunks_per_document <= 10:
            raise ValueError("max_chunks_per_document must be between 1 and 10")
        scoped_filters = dict(filters or {})
        scoped_filters["knowledge_base_id"] = knowledge_base_id
        query_entity_ids = extract_security_entity_ids(query)
        if enable_abstention and query_entity_ids and require_exact_entity_match:
            scoped_filters[ENTITY_IDS_ANY_FILTER] = query_entity_ids

        top_k = top_k or self._settings.search_top_k
        vector_top_k = vector_top_k or self._settings.search_vector_top_k
        keyword_top_k = keyword_top_k or self._settings.search_keyword_top_k
        fusion_method = fusion_method or self._settings.search_fusion_method
        vector_weight = vector_weight if vector_weight is not None else self._settings.search_vector_weight
        keyword_weight = keyword_weight if keyword_weight is not None else self._settings.search_keyword_weight
        component_max_retries = (
            self._settings.search_component_max_retries
            if component_max_retries is None
            else component_max_retries
        )
        if not 0 <= component_max_retries <= 5:
            raise ValueError("component_max_retries must be between 0 and 5")

        vector_results: list[dict[str, Any]] = []
        keyword_results: list[dict[str, Any]] = []

        tasks: list[
            tuple[
                RetrievalComponent,
                Callable[[], Awaitable[list[dict[str, Any]]]],
            ]
        ] = []
        if enable_vector:
            vector_retriever = (
                self._vector
                if embedding_profile == "configured"
                else get_vector_retriever(embedding_profile)
            )
            tasks.append(
                (
                    RetrievalComponent.VECTOR,
                    lambda: vector_retriever.retrieve(
                        query,
                        vector_top_k,
                        scoped_filters,
                    ),
                )
            )
        if enable_keyword:
            tasks.append(
                (
                    RetrievalComponent.KEYWORD,
                    lambda: self._keyword.retrieve(
                        query,
                        keyword_top_k,
                        scoped_filters,
                    ),
                )
            )

        if not tasks:
            raise ValueError("At least one retrieval backend must be enabled")

        degraded_components: list[RetrievalComponent] = []
        available_components: list[RetrievalComponent] = []
        component_attempts: dict[str, int] = {}
        recovered_components: list[str] = []
        if tasks:
            gathered: list[Any] = await asyncio.gather(
                *(
                    _retrieve_with_retry(
                        operation,
                        max_retries=component_max_retries,
                        backoff_seconds=(
                            self._settings.search_component_retry_backoff_seconds
                        ),
                    )
                    for _, operation in tasks
                )
            )
            for (name, _), (result, error, attempts) in zip(tasks, gathered):
                component_attempts[name.value] = attempts
                if error is not None:
                    degraded_components.append(name)
                    logger.warning(
                        "%s retrieval failed after %s attempt(s): %s",
                        name,
                        attempts,
                        error,
                        exc_info=(type(error), error, error.__traceback__),
                    )
                elif name == RetrievalComponent.VECTOR:
                    if attempts > 1:
                        recovered_components.append(name.value)
                    available_components.append(name)
                    vector_results = result
                else:
                    if attempts > 1:
                        recovered_components.append(name.value)
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
            dict.fromkeys(
                str(item.get("document_id"))
                for item in merged
                if item.get("document_id")
            )
        )
        ready_ids = await self._documents.ready_ids(document_ids, knowledge_base_id)
        merged = [
            item
            for item in merged
            if str(item.get("document_id")) in ready_ids
        ]
        merged = _promote_exact_entity_matches(merged, query_entity_ids)
        abstained = False
        abstention_reason: str | None = None
        active_min_vector_score: float | None = None
        if enable_abstention and query_entity_ids and require_exact_entity_match:
            merged = [
                item for item in merged if item.get("exact_entity_match") is not None
            ]
            if not merged:
                abstained = True
                abstention_reason = "no_exact_entity_match"
        elif (
            enable_abstention
            and enable_vector
            and RetrievalComponent.VECTOR in available_components
            and min_vector_score is not None
        ):
            active_min_vector_score = min_vector_score
            has_trusted_vector_anchor = any(
                item.get("vector_score") is not None
                and float(item["vector_score"]) >= min_vector_score
                for item in merged
            )
            if has_trusted_vector_anchor:
                merged = [
                    item
                    for item in merged
                    if item.get("keyword_score") is not None
                    or (
                        item.get("vector_score") is not None
                        and float(item["vector_score"]) >= min_vector_score
                    )
                ]
            else:
                merged = []
                abstained = True
                abstention_reason = "low_vector_score"
        merged, deduplicated_chunks = _deduplicate_by_document(
            merged,
            max_chunks_per_document=max_chunks_per_document,
        )

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
            merged = _promote_exact_entity_matches(merged, query_entity_ids)

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
            "query_entities": query_entity_ids,
            "max_chunks_per_document": max_chunks_per_document,
            "deduplicated_chunks": deduplicated_chunks,
            "abstained": abstained,
            "abstention_reason": abstention_reason,
            "min_vector_score": active_min_vector_score,
            "component_attempts": component_attempts,
            "recovered_components": recovered_components,
        }


async def _retrieve_with_retry(
    operation: Callable[[], Awaitable[list[dict[str, Any]]]],
    *,
    max_retries: int,
    backoff_seconds: float,
) -> tuple[list[dict[str, Any]], Exception | None, int]:
    """组件失败时有限重试；成功的空结果不会被误判为故障。"""
    for attempt in range(1, max_retries + 2):
        try:
            return await operation(), None, attempt
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if attempt > max_retries or getattr(error, "retryable", True) is False:
                return [], error, attempt
            if backoff_seconds > 0:
                await asyncio.sleep(backoff_seconds * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")


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


def _minmax_normalize(scores: list[float]) -> list[float]:
    """将一侧引擎分数缩放到 [0, 1]；全相等时置为 1，避免除零。"""
    if not scores:
        return []
    lo = min(scores)
    hi = max(scores)
    if hi <= lo:
        return [1.0 for _ in scores]
    span = hi - lo
    return [(s - lo) / span for s in scores]


def _weighted_score_fusion(
    vector_results: list[dict[str, Any]],
    keyword_results: list[dict[str, Any]],
    vector_weight: float,
    keyword_weight: float,
) -> list[dict[str, Any]]:
    """先对两侧原始分做 min-max，再按权重相加，避免 BM25 量级压倒余弦分。"""
    merged: dict[str, dict[str, Any]] = {}

    vector_norms = _minmax_normalize([float(item.get("score", 0) or 0) for item in vector_results])
    keyword_norms = _minmax_normalize([float(item.get("score", 0) or 0) for item in keyword_results])

    for item, norm in zip(vector_results, vector_norms):
        cid = item.get("chunk_id") or ""
        merged[cid] = {**item, "vector_score": item.get("score", 0), "keyword_score": None}
        merged[cid]["weighted_score"] = norm * vector_weight
        merged[cid]["score"] = merged[cid]["weighted_score"]

    for item, norm in zip(keyword_results, keyword_norms):
        cid = item.get("chunk_id") or ""
        ks = item.get("score", 0) or 0
        if cid in merged:
            for key, value in item.items():
                if merged[cid].get(key) is None and value is not None:
                    merged[cid][key] = value
            merged[cid]["keyword_score"] = ks
            merged[cid]["weighted_score"] = merged[cid].get("weighted_score", 0) + norm * keyword_weight
            merged[cid]["score"] = merged[cid]["weighted_score"]
        else:
            merged[cid] = {**item, "vector_score": None, "keyword_score": ks}
            merged[cid]["weighted_score"] = norm * keyword_weight
            merged[cid]["score"] = merged[cid]["weighted_score"]

    return sorted(merged.values(), key=lambda x: x.get("score", 0), reverse=True)


def _promote_exact_entity_matches(
    items: list[dict[str, Any]], query_entity_ids: list[str]
) -> list[dict[str, Any]]:
    """将主实体和关联实体精确命中稳定置顶，不改变组内原始顺序。"""
    if not query_entity_ids:
        return items
    promoted: list[tuple[int, int, dict[str, Any]]] = []
    for position, item in enumerate(items):
        priority = exact_entity_match_priority(item, query_entity_ids)
        item["exact_entity_match"] = (
            "primary" if priority == 2 else "related" if priority == 1 else None
        )
        promoted.append((priority, position, item))
    promoted.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in promoted]


def _deduplicate_by_document(
    items: list[dict[str, Any]],
    *,
    max_chunks_per_document: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    """稳定保留每篇文档排名最高的若干分块，并返回移除数量。"""
    kept: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for position, item in enumerate(items):
        document_id = item.get("document_id")
        group_key = (
            f"document:{document_id}"
            if document_id
            else f"chunk:{item.get('chunk_id') or item.get('_id') or position}"
        )
        count = counts.get(group_key, 0)
        if count >= max_chunks_per_document:
            continue
        counts[group_key] = count + 1
        kept.append(item)
    return kept, len(items) - len(kept)


def _format_results(merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in merged:
        security_fields = build_security_entity_fields(
            text=str(item.get("text") or ""),
            original_filename=item.get("original_filename"),
            metadata=item.get("metadata") or {},
        )
        for key in security_fields:
            if item.get(key):
                security_fields[key] = item[key]
        results.append(
            {
            "chunk_id": item.get("chunk_id") or item.get("_id", ""),
            "text": item.get("text") or "",
            "score": item.get("score", 0),
            "vector_score": item.get("vector_score"),
            "keyword_score": item.get("keyword_score"),
            "rerank_score": item.get("rerank_score"),
            **security_fields,
            "exact_entity_match": item.get("exact_entity_match"),
            "source": {
                "document_id": item.get("document_id", ""),
                "source_uri": item.get("source_uri", ""),
                "original_filename": item.get("original_filename"),
                "chunk_index": item.get("chunk_index", 0),
                "page_no": item.get("page_no"),
            },
            "metadata": item.get("metadata"),
        }
        )
    return results


def get_hybrid_search() -> HybridSearch:
    return HybridSearch()
