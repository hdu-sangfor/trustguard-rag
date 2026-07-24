"""知识库管理 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.core.embedding.profiles import get_embedding_profile
from app.schemas.knowledge_base import (
    KnowledgeBaseCreate,
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
)
from app.stores.knowledge_base_store import get_knowledge_base_store

router = APIRouter(prefix="/v1/knowledge-bases", tags=["knowledge-bases"])


def _response(row, document_count: int = 0) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=row.id,
        name=row.name,
        description=row.description,
        embedding_profile=row.embedding_profile,
        embedding_provider=row.embedding_provider,
        embedding_api_driver=row.embedding_api_driver,
        embedding_model=row.embedding_model,
        embedding_dim=row.embedding_dim,
        is_default=row.is_default,
        is_system=row.is_system,
        document_count=document_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases() -> KnowledgeBaseListResponse:
    store = get_knowledge_base_store()
    await store.get_default()
    rows = await store.list()
    return KnowledgeBaseListResponse(
        items=[_response(row, count) for row, count in rows], total=len(rows)
    )


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(request: KnowledgeBaseCreate) -> KnowledgeBaseResponse:
    try:
        profile = get_embedding_profile(request.embedding_profile)
        row = await get_knowledge_base_store().create(
            name=request.name,
            description=request.description,
            profile=profile,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _response(row)


@router.get("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(knowledge_base_id: str) -> KnowledgeBaseResponse:
    store = get_knowledge_base_store()
    row = await store.get(knowledge_base_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return _response(row, await store.document_count(row.id))


@router.patch("/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    knowledge_base_id: str, request: KnowledgeBaseUpdate
) -> KnowledgeBaseResponse:
    if not request.model_fields_set:
        raise HTTPException(status_code=400, detail="At least one editable field is required")
    try:
        row = await get_knowledge_base_store().update(
            knowledge_base_id, request.model_dump(exclude_unset=True)
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if row is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    store = get_knowledge_base_store()
    return _response(row, await store.document_count(row.id))


@router.delete("/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(knowledge_base_id: str) -> Response:
    try:
        deleted = await get_knowledge_base_store().delete(knowledge_base_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
