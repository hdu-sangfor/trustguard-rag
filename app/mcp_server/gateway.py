"""只读 MCP 协议适配：委托应用层检索并保留精确资源读取。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.application.knowledge import aggregate_revision
from app.mcp_server.backend import BackendError, RagBackend
from app.mcp_server.scopes import ScopeRegistry
from app.schemas.knowledge import (
    KnowledgeEffectiveness,
    KnowledgeResource,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceType,
    KnowledgeVisibility,
)


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


class KnowledgeGateway:
    def __init__(
        self,
        *,
        backend: RagBackend,
        scopes: ScopeRegistry,
        resource_max_chars: int = 32000,
    ) -> None:
        self._backend = backend
        self._scopes = scopes
        self._resource_max_chars = resource_max_chars

    async def search(
        self,
        request: KnowledgeSearchRequest,
        *,
        request_id: str | None = None,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> KnowledgeSearchResponse:
        active_request_id = request_id or f"req-{uuid4()}"
        try:
            payload = await self._backend.search_scope(
                request_id=active_request_id,
                payload=request.model_dump(mode="json"),
                workspace_id=workspace_id,
                allowed_workflow_types=allowed_workflow_types,
            )
        except BackendError as error:
            raise _gateway_error_from_backend(error, active_request_id) from error
        try:
            return KnowledgeSearchResponse.model_validate(payload)
        except ValidationError as error:
            raise KnowledgeGatewayError(
                "SCHEMA_MISMATCH",
                "RAG returned an invalid scope search response",
                retryable=False,
                request_id=active_request_id,
            ) from error

    async def read_resource_ref(
        self,
        *,
        scope: str,
        resource_ref: str,
        request_id: str | None = None,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> KnowledgeResource:
        active_request_id = request_id or f"req-{uuid4()}"
        try:
            payload = await self._backend.get_resource(
                scope=scope,
                resource_ref=resource_ref,
                request_id=active_request_id,
                workspace_id=workspace_id,
                allowed_workflow_types=allowed_workflow_types,
            )
        except BackendError as error:
            raise _gateway_error_from_backend(error, active_request_id) from error
        try:
            resource = KnowledgeResource.model_validate(payload)
        except ValidationError as error:
            raise KnowledgeGatewayError(
                "SCHEMA_MISMATCH",
                "RAG returned an invalid knowledge resource",
                retryable=False,
                request_id=active_request_id,
            ) from error
        return resource.model_copy(
            update={"text": resource.text[: self._resource_max_chars]}
        )

    async def read_resource(
        self,
        *,
        scope: str,
        chunk_id: str,
        revision: str,
        request_id: str | None = None,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
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
                self._backend.get_content_revision(
                    knowledge_base_id,
                    request_id=active_request_id,
                    workspace_id=workspace_id,
                    allowed_workflow_types=allowed_workflow_types,
                )
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
                    workspace_id=workspace_id,
                    allowed_workflow_types=allowed_workflow_types,
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
            source_revision=(
                payload.get("source_revision")
                if isinstance(payload.get("source_revision"), int)
                else None
            ),
            content_hash=(
                f"sha256:{payload['content_hash']}"
                if isinstance(payload.get("content_hash"), str)
                else None
            ),
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
