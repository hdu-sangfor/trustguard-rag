"""重排序模块：BGE / none / future providers。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class Reranker:
    """重排序门面：支持 bge（本地）、none（透传）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._provider = self._settings.rerank_provider.strip().lower()
        self._model = None

    async def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        top_k = top_k or self._settings.search_top_k or len(candidates)

        if self._provider == "none":
            return candidates[:top_k]

        if self._provider == "bge":
            return await self._bge_rerank(query, candidates, top_k)

        logger.warning("unknown rerank provider %s, returning candidates as-is", self._provider)
        return candidates[:top_k]

    async def _bge_rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        try:
            model = _get_bge_reranker(self._settings)
        except Exception as e:
            logger.warning("bge reranker unavailable: %s, returning unranked results", e)
            return candidates[:top_k]

        pairs = [[query, c["text"]] for c in candidates]
        scores = await asyncio.to_thread(model.compute_score, pairs)
        if isinstance(scores, float):
            scores = [scores]

        scored = list(zip(scores, candidates))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]
        for s, c in top:
            c["rerank_score"] = float(s)
        return [c for _, c in top]


def _get_bge_reranker(settings: Settings):
    global _RERANKER_CACHE, _RERANKER_KEY
    key = (settings.rerank_model, settings.rerank_device)
    if _RERANKER_CACHE is not None and _RERANKER_KEY == key:
        return _RERANKER_CACHE
    try:
        from FlagEmbedding import FlagReranker
    except ImportError:
        raise RuntimeError(
            "BGE reranker requires FlagEmbedding. "
            "Run 'pip install FlagEmbedding' or set RAG_RERANK_PROVIDER=none."
        )
    _RERANKER_CACHE = FlagReranker(
        settings.rerank_model,
        use_fp16=True,
        device=settings.rerank_device,
    )
    _RERANKER_KEY = key
    return _RERANKER_CACHE


_RERANKER_CACHE: Any = None
_RERANKER_KEY: tuple | None = None


def get_reranker() -> Reranker:
    return Reranker()
