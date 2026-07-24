"""可降级的查询意图识别、受控改写和检索预算规划。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from app.core.generation.llm_client import LLMClient, LLMError, normalize_llm_provider
from app.domain import RetrievalMode
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)
_INTENT_CACHE: OrderedDict[str, tuple[float, _Intent]] = OrderedDict()
_INTENT_CACHE_LOCK = threading.Lock()

_ENUMERATION_ACTION = re.compile(
    r"(有哪些|包括哪些|包含哪些|列出|罗列|枚举|逐条|全部|所有|分别|各自|一览)"
)
_ENUMERATION_TARGET = re.compile(
    r"(条款|规定|要求|措施|责任|义务|职责|类型|类别|步骤|阶段|方面|内容|条件)"
)
_COMPREHENSIVE = re.compile(
    r"(概括|总结|综述|整体|全面|主要内容|如何理解|从哪些方面|有何影响)"
)
_MULTI_EVIDENCE = re.compile(
    r"(分别|两个|两种|三种|多个|各自|对比|比较|区别|相同吗|"
    r"哪个.{0,30}哪个|从.{0,30}到.{0,30}(链|过程)|攻击链|不是一回事|"
    r"以及|及其|和.{0,30}(关系|差异|日期|公告))"
)
_FOCUSED = re.compile(
    r"(是什么|为什么|多少|多久|何时|什么时候|哪一号|谁负责|是否|能否|怎么修复)"
)


@dataclass(frozen=True)
class QueryPlan:
    mode: RetrievalMode
    source: str
    confidence: float
    scope: str
    target: str | None
    semantic_queries: tuple[str, ...]
    keyword_queries: tuple[str, ...]
    top_k: int
    vector_top_k: int
    keyword_top_k: int
    max_chunks_per_document: int
    rerank_candidate_top_k: int
    context_max_chunks: int
    adjacent_chunk_radius: int

    @property
    def coverage_status(self) -> str:
        return "partial" if self.mode == RetrievalMode.ENUMERATION else "not_applicable"

    @property
    def coverage_warning(self) -> str | None:
        if self.mode != RetrievalMode.ENUMERATION:
            return None
        return "结果来自相关性检索，不能保证覆盖知识库中的全部条款或项目。"

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.mode.value,
            "scope": self.scope,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "target": self.target,
            "semantic_queries": list(self.semantic_queries),
            "keyword_queries": list(self.keyword_queries),
            "effective_parameters": {
                "top_k": self.top_k,
                "vector_top_k": self.vector_top_k,
                "keyword_top_k": self.keyword_top_k,
                "max_chunks_per_document": self.max_chunks_per_document,
                "rerank_candidate_top_k": self.rerank_candidate_top_k,
                "context_max_chunks": self.context_max_chunks,
                "adjacent_chunk_radius": self.adjacent_chunk_radius,
            },
        }


@dataclass(frozen=True)
class _Intent:
    mode: RetrievalMode
    source: str
    confidence: float
    scope: str = "local"
    target: str | None = None
    semantic_queries: tuple[str, ...] = ()
    keyword_queries: tuple[str, ...] = ()


class QueryPlanner:
    """规则优先、LLM 补充，并对所有模型输出施加固定预算。"""

    def __init__(
        self,
        settings: Settings | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        planner_settings = self._settings.model_copy(
            update={
                "llm_timeout_seconds": self._settings.query_planner_timeout_seconds,
                "llm_temperature": 0.0,
                "llm_max_output_tokens": 512,
                "llm_json_response_format": True,
            }
        )
        self._llm = llm_client or LLMClient(planner_settings)

    async def plan(
        self,
        query: str,
        *,
        requested_mode: RetrievalMode = RetrievalMode.AUTO,
        enable_query_rewrite: bool = True,
        top_k: int | None = None,
        vector_top_k: int | None = None,
        keyword_top_k: int | None = None,
        max_chunks_per_document: int | None = None,
    ) -> QueryPlan:
        if not self._settings.query_planner_enabled:
            intent = _Intent(RetrievalMode.FOCUSED, "disabled", 1.0)
        elif requested_mode != RetrievalMode.AUTO:
            intent = _Intent(
                requested_mode,
                "explicit",
                1.0,
                "exhaustive" if requested_mode == RetrievalMode.ENUMERATION else "broad",
            )
        else:
            rule = _rule_intent(query)
            if rule is not None:
                intent = rule
            else:
                intent = await self._llm_or_fallback(query)

        if not enable_query_rewrite:
            intent = _Intent(
                mode=intent.mode,
                source=intent.source,
                confidence=intent.confidence,
                scope=intent.scope,
                target=intent.target,
            )
        return _apply_budget(
            intent,
            self._settings,
            top_k=top_k,
            vector_top_k=vector_top_k,
            keyword_top_k=keyword_top_k,
            max_chunks_per_document=max_chunks_per_document,
        )

    async def _llm_or_fallback(self, query: str) -> _Intent:
        if (
            not self._settings.query_planner_llm_enabled
            or normalize_llm_provider(self._settings.llm_provider) == "none"
        ):
            return _Intent(RetrievalMode.FOCUSED, "fallback", 0.5)
        cache_key = f"{self._settings.llm_model}\0{' '.join(query.split())}"
        cached = _get_cached_intent(cache_key, self._settings)
        if cached is not None:
            return _Intent(
                mode=cached.mode,
                source="cache",
                confidence=cached.confidence,
                scope=cached.scope,
                target=cached.target,
                semantic_queries=cached.semantic_queries,
                keyword_queries=cached.keyword_queries,
            )
        try:
            completion = await asyncio.wait_for(
                self._llm.complete(_planner_messages(query)),
                timeout=self._settings.query_planner_timeout_seconds,
            )
            intent = _parse_llm_intent(
                completion.content,
                self._settings.query_planner_max_rewritten_queries,
            )
            if intent.confidence < self._settings.query_planner_min_confidence:
                return _Intent(RetrievalMode.FOCUSED, "fallback", intent.confidence)
            _cache_intent(cache_key, intent, self._settings)
            return intent
        except (LLMError, asyncio.TimeoutError, ValueError, json.JSONDecodeError) as error:
            logger.warning("query planner LLM failed; using focused fallback: %s", error)
            return _Intent(RetrievalMode.FOCUSED, "fallback", 0.5)


def _rule_intent(query: str) -> _Intent | None:
    normalized = " ".join(query.strip().split())
    action = _ENUMERATION_ACTION.search(normalized)
    target = _ENUMERATION_TARGET.search(normalized)
    if action and (target or action.group(0) in {"全部", "所有", "逐条"}):
        return _Intent(
            RetrievalMode.ENUMERATION,
            "rule",
            0.95,
            "exhaustive",
            target.group(0) if target else None,
        )
    if _COMPREHENSIVE.search(normalized) or _MULTI_EVIDENCE.search(normalized):
        return _Intent(RetrievalMode.COMPREHENSIVE, "rule", 0.9, "broad")
    if _FOCUSED.search(normalized):
        return _Intent(RetrievalMode.FOCUSED, "rule", 0.9)
    return None


def _planner_messages(query: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是检索查询规划器。只输出 JSON 对象，不回答问题。"
                "intent 只能是 focused、comprehensive、enumeration；"
                "scope 只能是 local、broad、exhaustive；confidence 为 0 到 1。"
                "focused 仅用于单一事实且通常一条证据即可回答的问题；"
                "comprehensive 用于比较多个实体、分别解释多个概念、还原攻击链、"
                "跨日期或需要多条证据综合的问题；"
                "enumeration 用于开放式列出全部条款、要求、责任或项目的问题。"
                "semantic_queries 和 keyword_queries 各最多 3 条，必须保留原问题含义，"
                "不得添加问题中没有的事实。"
            ),
        },
        {
            "role": "user",
            "content": (
                "规划以下查询：\n"
                f"{query}\n"
                '返回字段：{"intent":"","scope":"","target":null,'
                '"semantic_queries":[],"keyword_queries":[],"confidence":0.0}'
            ),
        },
    ]


def _parse_llm_intent(content: str, max_queries: int) -> _Intent:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value)
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("planner response must be an object")
    mode = RetrievalMode(str(payload.get("intent", "")))
    if mode == RetrievalMode.AUTO:
        raise ValueError("planner cannot return auto")
    scope = str(payload.get("scope") or "local")
    if scope not in {"local", "broad", "exhaustive"}:
        raise ValueError("invalid planner scope")
    confidence = float(payload.get("confidence", 0.0))
    if not 0 <= confidence <= 1:
        raise ValueError("invalid planner confidence")
    semantic = _safe_queries(payload.get("semantic_queries"), max_queries)
    keyword = _safe_queries(payload.get("keyword_queries"), max_queries)
    target = payload.get("target")
    return _Intent(
        mode=mode,
        source="llm",
        confidence=confidence,
        scope=scope,
        target=str(target).strip()[:200] if target else None,
        semantic_queries=semantic,
        keyword_queries=keyword,
    )


def _safe_queries(value: Any, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        query = " ".join(item.split())[:500]
        if query and query not in result:
            result.append(query)
        if len(result) >= limit:
            break
    return tuple(result)


def _apply_budget(
    intent: _Intent,
    settings: Settings,
    *,
    top_k: int | None,
    vector_top_k: int | None,
    keyword_top_k: int | None,
    max_chunks_per_document: int | None,
) -> QueryPlan:
    defaults = {
        RetrievalMode.FOCUSED: (settings.search_top_k, settings.search_vector_top_k, settings.search_keyword_top_k, 3, settings.rerank_top_k, settings.answer_max_context_chunks, 0),
        RetrievalMode.COMPREHENSIVE: (15, 40, 40, 5, 25, 12, 1),
        RetrievalMode.ENUMERATION: (20, 80, 80, 10, 50, 20, 2),
    }
    planned = defaults[intent.mode]
    effective_top_k = top_k if top_k is not None else planned[0]
    return QueryPlan(
        mode=intent.mode,
        source=intent.source,
        confidence=intent.confidence,
        scope=intent.scope,
        target=intent.target,
        semantic_queries=intent.semantic_queries,
        keyword_queries=intent.keyword_queries,
        top_k=effective_top_k,
        vector_top_k=vector_top_k if vector_top_k is not None else planned[1],
        keyword_top_k=keyword_top_k if keyword_top_k is not None else planned[2],
        max_chunks_per_document=(
            max_chunks_per_document
            if max_chunks_per_document is not None
            else planned[3]
        ),
        rerank_candidate_top_k=max(planned[4], effective_top_k),
        context_max_chunks=max(settings.answer_max_context_chunks, planned[5]),
        adjacent_chunk_radius=planned[6],
    )


def get_query_planner() -> QueryPlanner:
    return QueryPlanner()


def _get_cached_intent(key: str, settings: Settings) -> _Intent | None:
    if settings.query_planner_cache_ttl_seconds <= 0:
        return None
    now = time.monotonic()
    with _INTENT_CACHE_LOCK:
        cached = _INTENT_CACHE.get(key)
        if cached is None:
            return None
        expires_at, intent = cached
        if expires_at <= now:
            _INTENT_CACHE.pop(key, None)
            return None
        _INTENT_CACHE.move_to_end(key)
        return intent


def _cache_intent(key: str, intent: _Intent, settings: Settings) -> None:
    if settings.query_planner_cache_ttl_seconds <= 0:
        return
    with _INTENT_CACHE_LOCK:
        _INTENT_CACHE[key] = (
            time.monotonic() + settings.query_planner_cache_ttl_seconds,
            intent,
        )
        _INTENT_CACHE.move_to_end(key)
        while len(_INTENT_CACHE) > settings.query_planner_cache_size:
            _INTENT_CACHE.popitem(last=False)
