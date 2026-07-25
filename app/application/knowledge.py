"""知识检索应用服务：统一解析、执行和响应组装语义。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.application.access import (
    KnowledgeAccessContext,
    KnowledgeAccessDenied,
    KnowledgePermission,
)
from app.application.scopes import ScopeRegistry
from app.core.retrieval.request_context import resolve_search_execution
from app.core.retrieval.search import SearchUnavailableError, get_hybrid_search
from app.domain import CoverageStatus, RetrievalMode, SearchStatus
from app.schemas.knowledge import (
    KnowledgeCoverage,
    KnowledgeEffectiveness,
    KnowledgeHit,
    KnowledgeSearchRequest as ScopeSearchRequest,
    KnowledgeSearchResponse as ScopeSearchResponse,
    KnowledgeSourceType,
    KnowledgeVisibility,
    McpQueryPlan,
    McpQueryPlanSource,
)
from app.schemas.search import SearchCoverage, SearchRequest, SearchResponse
from app.settings import get_settings
from app.stores.knowledge_base_store import get_knowledge_base_store

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|password|passwd|secret)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_INTENT_PRIORITY = {
    RetrievalMode.AUTO: 0,
    RetrievalMode.FOCUSED: 1,
    RetrievalMode.COMPREHENSIVE: 2,
    RetrievalMode.ENUMERATION: 3,
}


@dataclass(frozen=True)
class _SuccessfulScopeSearch:
    knowledge_base_id: str
    response: SearchResponse


class KnowledgeSearchError(RuntimeError):
    """可由不同北向协议映射的稳定应用层检索错误。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str = "INVALID_ARGUMENT",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


class KnowledgeApplicationService:
    """封装一次受知识库范围约束的检索执行。"""

    async def search(
        self,
        request: SearchRequest,
        *,
        request_id: str,
        access_context: KnowledgeAccessContext,
    ) -> SearchResponse:
        try:
            access_context.require(
                KnowledgePermission.SEARCH,
                knowledge_base_id=request.knowledge_base_id,
            )
        except KnowledgeAccessDenied as error:
            raise KnowledgeSearchError(str(error), status_code=403) from error
        try:
            context = await resolve_search_execution(request)
        except LookupError as error:
            raise KnowledgeSearchError(str(error), status_code=404) from error
        except ValueError as error:
            raise KnowledgeSearchError(str(error), status_code=400) from error

        try:
            result = await get_hybrid_search().search(**context.search_kwargs)
        except SearchUnavailableError as error:
            raise KnowledgeSearchError(str(error), status_code=503) from error

        coverage_status = result.get("coverage_status", "not_applicable")
        coverage_warning = result.get("coverage_warning")
        return SearchResponse(
            request_id=request_id,
            query=request.query,
            knowledge_base_id=context.knowledge_base.id,
            content_revision=context.knowledge_base.content_revision,
            search_status=result["search_status"],
            effective_mode=result["effective_mode"],
            results=result["results"],
            total=result["total"],
            fusion_method=result["fusion_method"],
            retrieval_time_ms=result["retrieval_time_ms"],
            components=result["components"],
            degraded_components=result["degraded_components"],
            query_entities=result.get("query_entities", []),
            max_chunks_per_document=result.get("max_chunks_per_document", 1),
            deduplicated_chunks=result.get("deduplicated_chunks", 0),
            abstained=result.get("abstained", False),
            abstention_reason=result.get("abstention_reason"),
            min_vector_score=result.get("min_vector_score"),
            component_attempts=result.get("component_attempts", {}),
            recovered_components=result.get("recovered_components", []),
            query_plan=result.get("query_plan") or {},
            coverage=SearchCoverage(
                status=coverage_status,
                warning=coverage_warning,
            ),
            coverage_status=coverage_status,
            coverage_warning=coverage_warning,
        )

    async def search_scope(
        self,
        request: ScopeSearchRequest,
        *,
        request_id: str,
        access_context: KnowledgeAccessContext,
        scopes: ScopeRegistry | None = None,
    ) -> ScopeSearchResponse:
        """在逻辑 Scope 内执行多知识库检索、RRF、去重和降级合并。"""
        started = time.perf_counter()
        try:
            access_context.require(KnowledgePermission.SEARCH)
        except KnowledgeAccessDenied as error:
            raise KnowledgeSearchError(
                str(error),
                status_code=403,
                code="AUTH_FORBIDDEN",
            ) from error

        settings = get_settings()
        registry = scopes or ScopeRegistry.from_json(settings.mcp_scope_mapping_json)
        try:
            definition = registry.require(request.scope)
        except LookupError as error:
            raise KnowledgeSearchError(
                "The requested knowledge scope is not configured",
                status_code=404,
                code="UNKNOWN_SCOPE",
            ) from error
        _validate_scope_filters(request, definition.allowed_content_types)
        try:
            for knowledge_base_id in definition.knowledge_base_ids:
                access_context.require(
                    KnowledgePermission.SEARCH,
                    knowledge_base_id=knowledge_base_id,
                )
        except KnowledgeAccessDenied as error:
            raise KnowledgeSearchError(
                str(error),
                status_code=403,
                code="AUTH_FORBIDDEN",
            ) from error

        query = redact_query(request.query)
        mode = (
            definition.default_mode
            if request.mode == RetrievalMode.AUTO
            and definition.default_mode != RetrievalMode.AUTO
            else request.mode
        )
        per_kb_limit = max(request.limit, definition.per_knowledge_base_limit)
        search_results = await asyncio.gather(
            *(
                self.search(
                    SearchRequest(
                        query=query,
                        knowledge_base_id=knowledge_base_id,
                        top_k=min(per_kb_limit, 100),
                        retrieval_mode=mode,
                        enable_query_rewrite=request.rewrite,
                        enable_vector=True,
                        enable_keyword=True,
                        enable_rerank=True,
                    ),
                    request_id=request_id,
                    access_context=access_context,
                )
                for knowledge_base_id in definition.knowledge_base_ids
            ),
            return_exceptions=True,
        )
        _propagate_cancellation(search_results)

        successful: list[_SuccessfulScopeSearch] = []
        failures: list[tuple[str, BaseException]] = []
        for knowledge_base_id, result in zip(
            definition.knowledge_base_ids,
            search_results,
        ):
            if isinstance(result, BaseException):
                failures.append((knowledge_base_id, result))
            elif isinstance(result, SearchResponse):
                successful.append(_SuccessfulScopeSearch(knowledge_base_id, result))

        if not successful:
            error = failures[0][1] if failures else None
            if isinstance(error, KnowledgeSearchError):
                raise error
            raise KnowledgeSearchError(
                "RAG service is unavailable",
                status_code=503,
                code="RAG_UNAVAILABLE",
                retryable=True,
            ) from error

        revisions: dict[str, int | str] = {
            item.knowledge_base_id: item.response.content_revision
            for item in successful
        }
        failed_revisions = await asyncio.gather(
            *(
                self._content_revision(knowledge_base_id)
                for knowledge_base_id, _ in failures
            ),
            return_exceptions=True,
        )
        _propagate_cancellation(failed_revisions)
        for (knowledge_base_id, _), result in zip(failures, failed_revisions):
            revisions[knowledge_base_id] = (
                result if isinstance(result, int) and result >= 0 else "unavailable"
            )

        revision = aggregate_revision(revisions)
        hits = _fuse_scope_hits(
            successful,
            request=request,
            revision=revision,
            rrf_k=settings.mcp_rrf_k,
            snippet_max_chars=settings.mcp_snippet_max_chars,
        )
        degraded = _degraded_components(
            successful,
            has_federation_failure=bool(failures),
        )
        return ScopeSearchResponse(
            schema_version="trustguard-knowledge-search-v1",
            request_id=request_id,
            scope=request.scope.value,
            status=SearchStatus.DEGRADED if degraded else SearchStatus.OK,
            content_revision=revision,
            hits=hits[: request.limit],
            query_plan=_combined_query_plan(successful, fallback_mode=mode),
            coverage=_combined_coverage(successful, bool(failures)),
            degraded_components=degraded,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def _content_revision(self, knowledge_base_id: str) -> int:
        knowledge_base = await get_knowledge_base_store().resolve(knowledge_base_id)
        return knowledge_base.content_revision


def get_knowledge_application_service() -> KnowledgeApplicationService:
    """返回无状态知识应用服务，便于 HTTP、MCP 后端和测试复用。"""
    return KnowledgeApplicationService()


def aggregate_revision(revisions: dict[str, int | str]) -> str:
    material = "\n".join(
        f"{knowledge_base_id}:{revisions[knowledge_base_id]}"
        for knowledge_base_id in sorted(revisions)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def redact_query(query: str) -> str:
    redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", query)
    return _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )


def _fuse_scope_hits(
    searches: list[_SuccessfulScopeSearch],
    *,
    request: ScopeSearchRequest,
    revision: str,
    rrf_k: int,
    snippet_max_chars: int,
) -> list[KnowledgeHit]:
    fused: dict[str, tuple[float, Any]] = {}
    for search in searches:
        filtered = [
            item for item in search.response.results if _matches_filters(item, request)
        ]
        for rank, item in enumerate(filtered, start=1):
            chunk_id = item.chunk_id
            rrf_score = 1.0 / (rrf_k + rank)
            previous = fused.get(chunk_id)
            if previous is None:
                fused[chunk_id] = (rrf_score, item)
            else:
                fused[chunk_id] = (previous[0] + rrf_score, previous[1])

    ordered = sorted(fused.items(), key=lambda row: row[1][0], reverse=True)
    hits: list[KnowledgeHit] = []
    for chunk_id, (score, item) in ordered:
        metadata = item.metadata or {}
        hits.append(
            KnowledgeHit(
                external_chunk_id=chunk_id,
                resource_uri=(
                    f"trustguard-rag://{quote(request.scope.value, safe='')}"
                    f"/chunks/{quote(chunk_id, safe='')}?revision={revision}"
                ),
                snippet=item.text[:snippet_max_chars],
                score=score,
                title=item.title,
                document_id=item.source.document_id,
                filename=item.source.original_filename,
                page_no=item.source.page_no,
                source_uri=item.source.source_uri,
                source_type=_source_type(metadata.get("source_type"), metadata),
                workflow_type=_optional_string(metadata.get("workflow_type")),
                effectiveness=_effectiveness(metadata.get("effectiveness")),
                visibility=_visibility(metadata.get("visibility")),
                expanded=item.expanded,
            )
        )
    return hits


def _validate_scope_filters(
    request: ScopeSearchRequest,
    allowed_content_types: list[str],
) -> None:
    requested = set(request.filters.content_types)
    allowed = set(allowed_content_types)
    if requested and (not allowed or not requested.issubset(allowed)):
        raise KnowledgeSearchError(
            "The requested content type filter is not allowed for this scope",
            status_code=400,
            code="INVALID_ARGUMENT",
        )


def _combined_query_plan(
    searches: list[_SuccessfulScopeSearch],
    *,
    fallback_mode: RetrievalMode,
) -> McpQueryPlan:
    candidates: list[tuple[RetrievalMode, McpQueryPlanSource]] = []
    for search in searches:
        plan = search.response.query_plan
        try:
            intent = RetrievalMode(str(plan.intent))
        except ValueError:
            continue
        candidates.append((intent, _query_plan_source(str(plan.source))))
    if not candidates:
        return McpQueryPlan(
            intent=fallback_mode,
            source=(
                McpQueryPlanSource.EXPLICIT
                if fallback_mode != RetrievalMode.AUTO
                else McpQueryPlanSource.HEURISTIC
            ),
        )
    intent, source = max(candidates, key=lambda item: _INTENT_PRIORITY[item[0]])
    return McpQueryPlan(intent=intent, source=source)


def _query_plan_source(value: Any) -> McpQueryPlanSource:
    if value == "explicit":
        return McpQueryPlanSource.EXPLICIT
    if value in {"llm", "cache"}:
        return McpQueryPlanSource.LLM
    return McpQueryPlanSource.HEURISTIC


def _combined_coverage(
    searches: list[_SuccessfulScopeSearch],
    has_failure: bool,
) -> KnowledgeCoverage:
    statuses: list[CoverageStatus] = []
    warnings: list[str] = []
    for search in searches:
        coverage = search.response.coverage
        statuses.append(coverage.status)
        if coverage.warning and coverage.warning not in warnings:
            warnings.append(coverage.warning)
    if has_failure:
        status = CoverageStatus.UNKNOWN
        warnings.append("部分知识库不可用，无法判断当前结果的完整覆盖程度。")
    elif CoverageStatus.PARTIAL in statuses:
        status = CoverageStatus.PARTIAL
    elif statuses and all(item == CoverageStatus.COMPLETE for item in statuses):
        status = CoverageStatus.COMPLETE
    elif CoverageStatus.UNKNOWN in statuses:
        status = CoverageStatus.UNKNOWN
    else:
        status = CoverageStatus.NOT_APPLICABLE
    return KnowledgeCoverage(
        status=status,
        warning=" ".join(warnings)[:1000] or None,
    )


def _degraded_components(
    searches: list[_SuccessfulScopeSearch],
    *,
    has_federation_failure: bool,
) -> list[str]:
    allowed = {"vector", "keyword", "rerank", "rewrite"}
    degraded: list[str] = []
    for search in searches:
        for value in search.response.degraded_components:
            if value in allowed and value not in degraded:
                degraded.append(value)
    if has_federation_failure:
        degraded.append("federation")
    return degraded


def _matches_filters(item: Any, request: ScopeSearchRequest) -> bool:
    metadata = item.metadata or {}
    source_type = _source_type(metadata.get("source_type"), metadata)
    if request.filters.source_types and source_type not in request.filters.source_types:
        return False
    if request.filters.content_types:
        content_type = metadata.get("content_type")
        if content_type not in request.filters.content_types:
            return False
    return True


def _source_type(value: Any, metadata: dict[str, Any]) -> KnowledgeSourceType:
    candidate = value or metadata.get("source_type")
    try:
        return KnowledgeSourceType(str(candidate))
    except ValueError:
        return KnowledgeSourceType.DOCUMENT


def _visibility(value: Any) -> KnowledgeVisibility:
    try:
        return KnowledgeVisibility(str(value))
    except ValueError:
        return KnowledgeVisibility.GLOBAL


def _effectiveness(value: Any) -> KnowledgeEffectiveness | None:
    if value is None:
        return None
    try:
        return KnowledgeEffectiveness(str(value))
    except ValueError:
        return KnowledgeEffectiveness.UNKNOWN


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _propagate_cancellation(values: list[Any] | tuple[Any, ...]) -> None:
    cancellation = next(
        (value for value in values if isinstance(value, asyncio.CancelledError)),
        None,
    )
    if cancellation is not None:
        raise cancellation
