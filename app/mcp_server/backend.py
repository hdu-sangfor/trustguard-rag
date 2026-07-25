"""MCP Gateway 到 RAG REST Core 的受控异步客户端。"""

from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

import httpx


class BackendError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code


class RagBackend(Protocol):
    async def search_scope(
        self,
        *,
        request_id: str,
        payload: dict[str, Any],
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, Any]: ...

    async def read_resource(
        self,
        *,
        scope: str,
        resource_ref: str,
        request_id: str,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, Any]: ...

    async def ready(self) -> bool: ...

    async def aclose(self) -> None: ...


class RestRagBackend:
    def __init__(
        self,
        *,
        base_url: str,
        internal_service_token: str | None,
        timeout_seconds: float,
    ) -> None:
        self._internal_service_token = internal_service_token
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    async def search_scope(
        self,
        *,
        request_id: str,
        payload: dict[str, Any],
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._request(
                "POST",
                "/v1/internal/knowledge/search-scope",
                headers=self._internal_headers(
                    request_id,
                    workspace_id=workspace_id,
                    allowed_workflow_types=allowed_workflow_types,
                ),
                json=payload,
            )
        except BackendError as error:
            if error.status_code == 404:
                raise BackendError(
                    "UNKNOWN_SCOPE",
                    "The requested knowledge scope is not configured",
                    retryable=False,
                    status_code=404,
                ) from error
            raise
        return _json_object(response)

    async def read_resource(
        self,
        *,
        scope: str,
        resource_ref: str,
        request_id: str,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            (
                "/v1/internal/knowledge/resources/"
                f"{quote(resource_ref, safe='')}?scope={quote(scope, safe='')}"
            ),
            headers=self._internal_headers(
                request_id,
                workspace_id=workspace_id,
                allowed_workflow_types=allowed_workflow_types,
            ),
        )
        return _json_object(response)

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()

    def _internal_headers(
        self,
        request_id: str,
        *,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, str]:
        if not self._internal_service_token:
            raise BackendError(
                "RAG_UNAVAILABLE",
                "RAG internal service authentication is not configured",
                retryable=False,
            )
        headers = {
            "Authorization": f"Bearer {self._internal_service_token}",
            "X-Request-ID": request_id,
        }
        if workspace_id:
            headers["X-TrustGuard-Workspace-ID"] = workspace_id
        if allowed_workflow_types is not None:
            headers["X-TrustGuard-Workflow-Types"] = ",".join(
                sorted(allowed_workflow_types)
            )
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                path,
                headers=headers,
                json=json,
            )
        except httpx.TimeoutException as error:
            raise BackendError(
                "RAG_TIMEOUT",
                "RAG request timed out",
                retryable=True,
            ) from error
        except httpx.HTTPError as error:
            raise BackendError(
                "RAG_UNAVAILABLE",
                "RAG service is unavailable",
                retryable=True,
            ) from error

        if response.status_code >= 500:
            raise BackendError(
                "RAG_UNAVAILABLE",
                "RAG service is unavailable",
                retryable=True,
                status_code=response.status_code,
            )
        if response.status_code in {401, 403} and path.startswith("/v1/internal/"):
            raise BackendError(
                "RAG_UNAVAILABLE",
                "RAG internal service authentication failed",
                retryable=False,
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise _backend_error_from_response(response)
        return response


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise BackendError(
            "SCHEMA_MISMATCH",
            "RAG returned invalid JSON",
            retryable=False,
        ) from error
    if not isinstance(payload, dict):
        raise BackendError(
            "SCHEMA_MISMATCH",
            "RAG returned an invalid response shape",
            retryable=False,
        )
    return payload


def _backend_error_from_response(response: httpx.Response) -> BackendError:
    code = "INVALID_ARGUMENT"
    message = "RAG rejected the gateway request"
    retryable = False
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        if isinstance(payload.get("code"), str):
            code = payload["code"]
        if isinstance(payload.get("message"), str):
            message = payload["message"]
        if isinstance(payload.get("retryable"), bool):
            retryable = payload["retryable"]
    return BackendError(
        code,
        message,
        retryable=retryable,
        status_code=response.status_code,
    )
