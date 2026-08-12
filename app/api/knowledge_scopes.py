"""Administration API for persisted logical Knowledge Scope policy."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.schemas.knowledge import KnowledgeScope
from app.schemas.knowledge_scope import (
    KnowledgeScopeListResponse,
    KnowledgeScopeResponse,
    KnowledgeScopeUpdate,
)
from app.settings import get_settings
from app.stores.experience_store import ensure_penetration_experience_knowledge_base
from app.stores.knowledge_scope_store import get_knowledge_scope_store

router = APIRouter(prefix="/v1/knowledge-scopes", tags=["knowledge-scopes"])


async def _sync_system_bindings(scope: KnowledgeScope | None = None) -> None:
    settings = get_settings()
    if settings.experience_enabled and scope in {None, KnowledgeScope.PENETRATION}:
        await ensure_penetration_experience_knowledge_base()


@router.get("", response_model=KnowledgeScopeListResponse)
async def list_knowledge_scopes() -> KnowledgeScopeListResponse:
    await _sync_system_bindings()
    rows = await get_knowledge_scope_store().list(
        include_experience=get_settings().experience_enabled
    )
    return KnowledgeScopeListResponse(items=rows, total=len(rows))


@router.get("/{scope}", response_model=KnowledgeScopeResponse)
async def get_knowledge_scope(scope: KnowledgeScope) -> KnowledgeScopeResponse:
    await _sync_system_bindings(scope)
    row = await get_knowledge_scope_store().get_response(
        scope,
        include_experience=get_settings().experience_enabled,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge scope is not configured")
    return row


@router.put("/{scope}", response_model=KnowledgeScopeResponse)
async def replace_knowledge_scope(
    scope: KnowledgeScope,
    request: KnowledgeScopeUpdate,
) -> KnowledgeScopeResponse:
    await _sync_system_bindings(scope)
    try:
        return await get_knowledge_scope_store().replace_manual(scope, request)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete(
    "/{scope}",
    response_model=KnowledgeScopeResponse,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Scope configuration removed"}},
)
async def clear_knowledge_scope(scope: KnowledgeScope) -> KnowledgeScopeResponse | Response:
    await _sync_system_bindings(scope)
    row = await get_knowledge_scope_store().clear_manual(scope)
    if row is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return row
