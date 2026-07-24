"""只读 MCP 知识能力：Scope 联邦检索、RRF 融合和精确资源读取。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from app.domain import CoverageStatus, RetrievalMode, SearchStatus
from app.mcp_server.backend import BackendError, RagBackend
from app.mcp_server.models import (
    KnowledgeCoverage,
    KnowledgeEffectiveness,
    KnowledgeHit,
    KnowledgeResource,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceType,
    KnowledgeVisibility,
    McpQueryPlan,
    McpQueryPlanSource,
)
from app.mcp_server.scopes import ScopeRegistry

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


class KnowledgeGatewayError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.request_id = request_id
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "trustguard-knowledge-error-v1",
            "request_id": self.request_id,
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "details": self.details,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class _SuccessfulSearch:
    knowledge_base_id: str
    payload: dict[str, Any]


class KnowledgeGateway:
    def __init__(
        self,
        *,
        backend: RagBackend,
        scopes: ScopeRegistry,
        rrf_k: int = 60,
        snippet_max_chars: int = 4000,
        resource_max_chars: int = 32000,
    ) -> None:
        self._backend = backend
        self._scopes = scopes
        self._rrf_k = rrf_k
        self._snippet_max_chars = snippet_max_chars
        self._resource_max_chars = resource_max_chars

    async def search(
        self,
        request: KnowledgeSearchRequest,
        *,
        request_id: str | None = None,
    ) -> KnowledgeSearchResponse:
        started = time.perf_counter()
        active_request_id = request_id or f"req-{uuid4()}"
        try:
            definition = self._scopes.require(request.scope)
        except LookupError as error:
            raise KnowledgeGatewayError(
                "UNKNOWN_SCOPE",
                "The requested knowledge scope is not configured",
                retryable=False,
                request_id=active_request_id,
            ) from error
        self._validate_filters(request, definition.allowed_content_types, active_request_id)

        query = redact_query(request.query)
        mode = (
            definition.default_mode
            if request.mode == RetrievalMode.AUTO
            and definition.default_mode != RetrievalMode.AUTO
            else request.mode
        )
        per_kb_limit = max(request.limit, definition.per_knowledge_base_limit)
        payload = {
            "query": query,
            "top_k": min(per_kb_limit, 100),
            "retrieval_mode": mode.value,
            "enable_query_rewrite": request.rewrite,
            "enable_vector": True,
            "enable_keyword": True,
            "enable_rerank": True,
        }
        gathered = await asyncio.gather(
            *(
                self._backend.search(
                    knowledge_base_id=knowledge_base_id,
                    request_id=active_request_id,
                    payload=payload,
                )
                for knowledge_base_id in definition.knowledge_base_ids
            ),
            return_exceptions=True,
        )
        _propagate_cancellation(gathered)

        successful: list[_SuccessfulSearch] = []
        failures: list[tuple[str, BaseException]] = []
        for knowledge_base_id, result in zip(
            definition.knowledge_base_ids, gathered
        ):
            if isinstance(result, BaseException):
                failures.append((knowledge_base_id, result))
            elif isinstance(result, dict):
                successful.append(_SuccessfulSearch(knowledge_base_id, result))

        if not successful:
            error = failures[0][1] if failures else None
            raise _gateway_error_from_backend(error, active_request_id)

        revisions = {
            item.knowledge_base_id: _valid_revision(item.payload.get("content_revision"))
            for item in successful
        }
        if failures:
            missing_revisions = await asyncio.gather(
                *(
                    self._backend.get_content_revision(knowledge_base_id)
                    for knowledge_base_id, _ in failures
                ),
                return_exceptions=True,
            )
            _propagate_cancellation(missing_revisions)
            for (knowledge_base_id, _), revision in zip(
                failures, missing_revisions
            ):
                revisions[knowledge_base_id] = (
                    revision if isinstance(revision, int) else "unavailable"
                )

        hits = self._fuse_hits(
            successful,
            request=request,
            revision=aggregate_revision(revisions),
        )
        degraded = _degraded_components(successful, has_federation_failure=bool(failures))
        status = SearchStatus.DEGRADED if degraded else SearchStatus.OK
        return KnowledgeSearchResponse(
            schema_version="trustguard-knowledge-search-v1",
            request_id=active_request_id,
            scope=request.scope.value,
            status=status,
            content_revision=aggregate_revision(revisions),
            hits=hits[: request.limit],
            query_plan=_combined_query_plan(successful, fallback_mode=mode),
            coverage=_combined_coverage(successful, bool(failures)),
            degraded_components=degraded,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def read_resource(
        self,
        *,
        scope: str,
        chunk_id: str,
        revision: str,
        request_id: str | None = None,
    ) -> KnowledgeResource:
        active_request_id = request_id or f"req-{uuid4()}"
        try:
            definition = self._scopes.require(scope)
        except LookupError as error:
            raise KnowledgeGatewayError(
                "UNKNOWN_SCOPE",
                "The requested knowledge scope is not configured",
                retryable=False,
                request_id=active_request_id,
            ) from error

        revision_results = await asyncio.gather(
            *(
                self._backend.get_content_revision(knowledge_base_id)
                for knowledge_base_id in definition.knowledge_base_ids
            ),
            return_exceptions=True,
        )
        _propagate_cancellation(revision_results)
        if any(isinstance(item, BaseException) for item in revision_results):
            error = next(
                item for item in revision_results if isinstance(item, BaseException)
            )
            raise _gateway_error_from_backend(error, active_request_id)
        current_revision = aggregate_revision(
            {
                knowledge_base_id: int(value)
                for knowledge_base_id, value in zip(
                    definition.knowledge_base_ids, revision_results
                )
            }
        )
        if revision != current_revision:
            raise KnowledgeGatewayError(
                "RESOURCE_STALE",
                "The knowledge resource revision is stale",
                retryable=False,
                request_id=active_request_id,
                details={
                    "requested_revision": revision,
                    "current_revision": current_revision,
                },
            )

        gathered = await asyncio.gather(
            *(
                self._backend.get_chunk(
                    knowledge_base_id=knowledge_base_id,
                    chunk_id=chunk_id,
                    request_id=active_request_id,
                )
                for knowledge_base_id in definition.knowledge_base_ids
            ),
            return_exceptions=True,
        )
        _propagate_cancellation(gathered)
        failures = [item for item in gathered if isinstance(item, BaseException)]
        matches = [item for item in gathered if isinstance(item, dict)]
        if not matches:
            if failures and len(failures) == len(gathered):
                raise _gateway_error_from_backend(failures[0], active_request_id)
            raise KnowledgeGatewayError(
                "RESOURCE_NOT_FOUND",
                "Knowledge resource not found",
                retryable=False,
                request_id=active_request_id,
            )
        payload = matches[0]
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        source_type = _source_type(payload.get("source_type"), metadata)
        return KnowledgeResource(
            schema_version="trustguard-knowledge-resource-v1",
            scope=scope,
            content_revision=current_revision,
            chunk_id=str(payload.get("chunk_id") or chunk_id),
            document_id=_optional_string(payload.get("document_id")),
            experience_id=_optional_string(metadata.get("experience_id")),
            text=str(payload.get("text") or "")[: self._resource_max_chars],
            title=_optional_string(payload.get("title")),
            filename=_optional_string(payload.get("filename")),
            page_no=_optional_page(payload.get("page_no")),
            source_uri=_optional_string(payload.get("source_uri")),
            source_type=source_type,
            workflow_type=_optional_string(metadata.get("workflow_type")),
            effectiveness=_effectiveness(metadata.get("effectiveness")),
            visibility=_visibility(metadata.get("visibility")),
            metadata=metadata,
        )

    def _fuse_hits(
        self,
        searches: list[_SuccessfulSearch],
        *,
        request: KnowledgeSearchRequest,
        revision: str,
    ) -> list[KnowledgeHit]:
        fused: dict[str, tuple[float, dict[str, Any]]] = {}
        for search in searches:
            raw_results = search.payload.get("results")
            results = raw_results if isinstance(raw_results, list) else []
            filtered = [
                item
                for item in results
                if isinstance(item, dict) and _matches_filters(item, request)
            ]
            for rank, item in enumerate(filtered, start=1):
                chunk_id = str(item.get("chunk_id") or "")
                if not chunk_id:
                    continue
                rrf_score = 1.0 / (self._rrf_k + rank)
                previous = fused.get(chunk_id)
                if previous is None:
                    fused[chunk_id] = (rrf_score, item)
                else:
                    fused[chunk_id] = (previous[0] + rrf_score, previous[1])

        ordered = sorted(fused.items(), key=lambda row: row[1][0], reverse=True)
        hits: list[KnowledgeHit] = []
        for chunk_id, (score, item) in ordered:
            source = item.get("source")
            source = source if isinstance(source, dict) else {}
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            hits.append(
                KnowledgeHit(
                    external_chunk_id=chunk_id,
                    resource_uri=(
                        f"trustguard-rag://{quote(request.scope.value, safe='')}"
                        f"/chunks/{quote(chunk_id, safe='')}?revision={revision}"
                    ),
                    snippet=str(item.get("text") or "")[: self._snippet_max_chars],
                    score=score,
                    title=_optional_string(item.get("title")),
                    document_id=_optional_string(source.get("document_id")),
                    filename=_optional_string(source.get("original_filename")),
                    page_no=_optional_page(source.get("page_no")),
                    source_uri=_optional_string(source.get("source_uri")),
                    source_type=_source_type(metadata.get("source_type"), metadata),
                    workflow_type=_optional_string(metadata.get("workflow_type")),
                    effectiveness=_effectiveness(metadata.get("effectiveness")),
                    visibility=_visibility(metadata.get("visibility")),
                    expanded=bool(item.get("expanded", False)),
                )
            )
        return hits

    @staticmethod
    def _validate_filters(
        request: KnowledgeSearchRequest,
        allowed_content_types: list[str],
        request_id: str,
    ) -> None:
        requested = set(request.filters.content_types)
        allowed = set(allowed_content_types)
        if requested and (not allowed or not requested.issubset(allowed)):
            raise KnowledgeGatewayError(
                "INVALID_ARGUMENT",
                "The requested content type filter is not allowed for this scope",
                retryable=False,
                request_id=request_id,
            )


def aggregate_revision(revisions: dict[str, int | str]) -> str:
    material = "\n".join(
        f"{knowledge_base_id}:{revisions[knowledge_base_id]}"
        for knowledge_base_id in sorted(revisions)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def redact_query(query: str) -> str:
    redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", query)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)


def _gateway_error_from_backend(
    error: BaseException | None,
    request_id: str,
) -> KnowledgeGatewayError:
    if isinstance(error, BackendError):
        return KnowledgeGatewayError(
            error.code,
            str(error),
            retryable=error.retryable,
            request_id=request_id,
        )
    return KnowledgeGatewayError(
        "RAG_UNAVAILABLE",
        "RAG service is unavailable",
        retryable=True,
        request_id=request_id,
    )


def _valid_revision(value: Any) -> int | str:
    return value if isinstance(value, int) and value >= 0 else "unknown"


def _combined_query_plan(
    searches: list[_SuccessfulSearch],
    *,
    fallback_mode: RetrievalMode,
) -> McpQueryPlan:
    candidates: list[tuple[RetrievalMode, McpQueryPlanSource]] = []
    for search in searches:
        plan = search.payload.get("query_plan")
        if not isinstance(plan, dict):
            continue
        try:
            intent = RetrievalMode(str(plan.get("intent")))
        except ValueError:
            continue
        candidates.append((intent, _query_plan_source(plan.get("source"))))
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
    searches: list[_SuccessfulSearch],
    has_failure: bool,
) -> KnowledgeCoverage:
    statuses: list[CoverageStatus] = []
    warnings: list[str] = []
    for search in searches:
        coverage = search.payload.get("coverage")
        if isinstance(coverage, dict):
            raw_status = coverage.get("status")
            warning = coverage.get("warning")
        else:
            raw_status = search.payload.get("coverage_status")
            warning = search.payload.get("coverage_warning")
        try:
            statuses.append(CoverageStatus(str(raw_status)))
        except ValueError:
            statuses.append(CoverageStatus.UNKNOWN)
        if isinstance(warning, str) and warning and warning not in warnings:
            warnings.append(warning)
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
    searches: list[_SuccessfulSearch],
    *,
    has_federation_failure: bool,
) -> list[str]:
    allowed = {"vector", "keyword", "rerank", "rewrite"}
    degraded: list[str] = []
    for search in searches:
        values = search.payload.get("degraded_components")
        if not isinstance(values, list):
            continue
        for value in values:
            if value in allowed and value not in degraded:
                degraded.append(value)
    if has_federation_failure:
        degraded.append("federation")
    return degraded


def _matches_filters(
    item: dict[str, Any],
    request: KnowledgeSearchRequest,
) -> bool:
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
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


def _optional_page(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 1 else None


def _propagate_cancellation(values: list[Any] | tuple[Any, ...]) -> None:
    cancellation = next(
        (value for value in values if isinstance(value, asyncio.CancelledError)),
        None,
    )
    if cancellation is not None:
        raise cancellation
