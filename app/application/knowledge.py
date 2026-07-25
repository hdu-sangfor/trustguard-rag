"""知识检索应用服务：统一解析、执行和响应组装语义。"""

from __future__ import annotations

from app.application.access import (
    KnowledgeAccessContext,
    KnowledgeAccessDenied,
    KnowledgePermission,
)
from app.core.retrieval.request_context import resolve_search_execution
from app.core.retrieval.search import SearchUnavailableError, get_hybrid_search
from app.schemas.search import SearchCoverage, SearchRequest, SearchResponse


class KnowledgeSearchError(RuntimeError):
    """可由不同北向协议映射的稳定应用层检索错误。"""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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


def get_knowledge_application_service() -> KnowledgeApplicationService:
    """返回无状态知识应用服务，便于 HTTP、MCP 后端和测试复用。"""
    return KnowledgeApplicationService()
