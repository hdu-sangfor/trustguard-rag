"""MCP Gateway 到 RAG REST Core 的受控异步客户端。"""

from __future__ import annotations

from typing import Any, Protocol

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
    ) -> dict[str, Any]: ...

    async def search(
        self,
        *,
        knowledge_base_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def get_chunk(
        self,
        *,
        knowledge_base_id: str,
        chunk_id: str,
        request_id: str,
    ) -> dict[str, Any] | None: ...

    async def get_content_revision(self, knowledge_base_id: str) -> int: ...

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
    ) -> dict[str, Any]:
        try:
            response = await self._request(
                "POST",
                "/v1/internal/knowledge/search-scope",
                headers=self._internal_headers(request_id),
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

    async def search(
        self,
        *,
        knowledge_base_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/v1/internal/knowledge/search",
            headers=self._internal_headers(request_id),
            json={**payload, "knowledge_base_id": knowledge_base_id},
        )
        return _json_object(response)

    async def get_chunk(
        self,
        *,
        knowledge_base_id: str,
        chunk_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            f"/v1/internal/knowledge-bases/{knowledge_base_id}/chunks/{chunk_id}",
            headers=self._internal_headers(request_id),
            allow_not_found=True,
        )
        return None if response is None else _json_object(response)

    async def get_content_revision(self, knowledge_base_id: str) -> int:
        response = await self._request(
            "GET",
            f"/v1/knowledge-bases/{knowledge_base_id}",
        )
        payload = _json_object(response)
        revision = payload.get("content_revision")
        if not isinstance(revision, int) or revision < 0:
            raise BackendError(
                "SCHEMA_MISMATCH",
                "RAG returned an invalid content revision",
                retryable=False,
            )
        return revision

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()

    def _internal_headers(self, request_id: str) -> dict[str, str]:
        if not self._internal_service_token:
            raise BackendError(
                "RAG_UNAVAILABLE",
                "RAG internal service authentication is not configured",
                retryable=False,
            )
        return {
            "Authorization": f"Bearer {self._internal_service_token}",
            "X-Request-ID": request_id,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
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

        if allow_not_found and response.status_code == 404:
            return None
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
            raise BackendError(
                "INVALID_ARGUMENT",
                "RAG rejected the gateway request",
                retryable=False,
                status_code=response.status_code,
            )
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
