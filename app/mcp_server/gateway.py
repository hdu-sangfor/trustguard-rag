"""只读 MCP 协议适配：校验共享检索与资源契约。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.mcp_server.backend import BackendError, RagBackend
from app.schemas.knowledge import (
    KnowledgeResource,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
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
        resource_max_chars: int = 32000,
    ) -> None:
        self._backend = backend
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

    async def read_resource(
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
            payload = await self._backend.read_resource(
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
