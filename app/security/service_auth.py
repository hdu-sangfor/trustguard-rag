"""Gateway 与 MCP 内部调用使用的独立服务身份。"""

from __future__ import annotations

import secrets
import re
from typing import Annotated

from fastapi import Header, HTTPException, Request

from app.application.access import (
    KnowledgeAccessContext,
    gateway_access_context,
    mcp_access_context,
)
from app.settings import get_settings

_CONTEXT_VALUE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _verify_bearer(
    authorization: str | None,
    *,
    expected: str | None,
    unconfigured_message: str,
) -> None:
    if not expected:
        raise HTTPException(status_code=503, detail=unconfigured_message)
    scheme, separator, credential = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not secrets.compare_digest(credential, expected)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid service credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_gateway_service(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> KnowledgeAccessContext:
    """验证 Agent Gateway 身份；开发模式可显式关闭以保留本地直连。"""
    settings = get_settings()
    if settings.gateway_auth_enabled:
        _verify_bearer(
            authorization,
            expected=settings.gateway_service_token,
            unconfigured_message="Gateway service authentication is not configured",
        )
        context = gateway_access_context(
            service_id="trustguard-agent-gateway",
            workspace_id=settings.default_workspace_id,
        )
    else:
        context = gateway_access_context(
            service_id="development-direct-rest",
            workspace_id=settings.default_workspace_id,
            development=True,
        )
    request.state.knowledge_access_context = context
    return context


async def require_internal_service(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    workspace_id: Annotated[
        str | None,
        Header(alias="X-TrustGuard-Workspace-ID"),
    ] = None,
    workflow_types: Annotated[
        str | None,
        Header(alias="X-TrustGuard-Workflow-Types"),
    ] = None,
) -> KnowledgeAccessContext:
    """验证 rag-mcp 调用 RAG 内部 Search/Resource API 的服务身份。"""
    settings = get_settings()
    _verify_bearer(
        authorization,
        expected=settings.internal_service_token,
        unconfigured_message="Internal service authentication is not configured",
    )
    trusted_workspace_id = workspace_id or settings.default_workspace_id
    if (
        not _CONTEXT_VALUE.fullmatch(trusted_workspace_id)
        or trusted_workspace_id != settings.default_workspace_id
    ):
        raise HTTPException(status_code=403, detail="Unsupported workspace context")
    trusted_workflow_types = frozenset(
        item.strip()
        for item in (workflow_types or "").split(",")
        if item.strip() and _CONTEXT_VALUE.fullmatch(item.strip())
    )
    context = mcp_access_context(
        service_id="trustguard-rag-mcp",
        workspace_id=trusted_workspace_id,
        allowed_workflow_types=trusted_workflow_types,
    )
    request.state.knowledge_access_context = context
    return context
