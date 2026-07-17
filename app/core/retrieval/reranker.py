"""重排序模块：支持本地、API 和禁用三种提供方。"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class RerankError(RuntimeError):
    """重排序提供方无法返回有效结果。"""


def normalize_rerank_provider(provider: str) -> str:
    """将外部提供方名称归一化为与嵌入模块一致的分发值。"""
    value = provider.strip().lower()
    if value in {"api", "openai", "openai_compatible", "remote", "bailian", "dashscope"}:
        return "api"
    if value in {"local", "bge"}:
        return "local"
    if value in {"none", "disabled", "off"}:
        return "none"
    raise RerankError(f"Unsupported rerank provider: {provider}")


def build_rerank_url(base_url: str) -> str:
    """构建重排序端点，并纠正误用的百炼嵌入兼容路径。"""
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    wrong_suffix = "/compatible-mode/v1"
    if (
        parsed.hostname
        and parsed.hostname.endswith(".maas.aliyuncs.com")
        and parsed.path.rstrip("/").endswith(wrong_suffix)
    ):
        corrected_path = parsed.path.rstrip("/")[: -len(wrong_suffix)] + "/compatible-api/v1"
        normalized = urlunsplit(
            (parsed.scheme, parsed.netloc, corrected_path, parsed.query, parsed.fragment)
        )
        logger.warning(
            "检测到百炼 Embedding 兼容地址，已自动改用 Rerank 地址：%s",
            corrected_path,
        )
    return f"{normalized}/reranks"


class Reranker:
    """重排序门面：支持 BGE 本地模型、百炼 API 和禁用时透传。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._provider = normalize_rerank_provider(self._settings.rerank_provider)
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

        if self._provider == "local":
            return await self._bge_rerank(query, candidates, top_k)

        return await self._api_rerank(query, candidates, top_k)

    async def _bge_rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        try:
            model = _get_bge_reranker(self._settings)
            pairs = [[query, c.get("text") or ""] for c in candidates]
            scores = await asyncio.to_thread(model.compute_score, pairs)
            if isinstance(scores, float):
                scores = [scores]

            scored = list(zip(scores, candidates))
            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:top_k]
            for score, candidate in top:
                candidate["rerank_score"] = float(score)
            return [candidate for _, candidate in top]
        except Exception as e:
            raise RerankError("BGE reranker is unavailable") from e

    async def _api_rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        """调用百炼兼容 OpenAI 协议的重排序 API，并按原始索引映射候选结果。"""
        try:
            return await self._call_api(query, candidates, top_k)
        except RerankError:
            raise
        except Exception as e:  # noqa: BLE001
            raise RerankError("API reranker is unavailable") from e

    async def _call_api(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        base_url = self._settings.rerank_base_url
        if not base_url:
            raise RerankError("RAG_RERANK_BASE_URL is required")
        api_key = self._settings.rerank_api_key or os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            raise RerankError("RAG_RERANK_API_KEY or DASHSCOPE_API_KEY is required")

        expected_count = min(top_k, len(candidates))
        payload: dict[str, Any] = {
            "model": self._settings.rerank_model,
            "query": query,
            "documents": [candidate.get("text") or "" for candidate in candidates],
            "top_n": expected_count,
        }
        if self._settings.rerank_instruction:
            payload["instruct"] = self._settings.rerank_instruction

        url = build_rerank_url(base_url)
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(
            timeout=self._settings.rerank_api_timeout_seconds
        ) as client:
            response = await client.post(url, json=payload, headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise RerankError(
                    f"Rerank API request failed with HTTP {response.status_code}"
                ) from e

        try:
            results = response.json()["results"]
        except (KeyError, TypeError, ValueError) as e:
            raise RerankError("Rerank API returned an invalid response") from e

        ranked: list[dict[str, Any]] = []
        seen_indexes: set[int] = set()
        try:
            for item in results:
                index = int(item["index"])
                score = float(item["relevance_score"])
                if index < 0 or index >= len(candidates) or index in seen_indexes:
                    raise ValueError(f"invalid result index: {index}")
                seen_indexes.add(index)
                candidate = {**candidates[index], "rerank_score": score}
                ranked.append(candidate)
        except (KeyError, TypeError, ValueError) as e:
            raise RerankError("Rerank API returned invalid result items") from e

        if len(ranked) != expected_count:
            raise RerankError(
                "Rerank API returned an unexpected number of results: "
                f"expected {expected_count}, got {len(ranked)}"
            )
        return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)


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
