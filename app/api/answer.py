"""基于检索证据生成回答的 HTTP API。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.application.access import (
    KnowledgeAccessContext,
    KnowledgeAccessDenied,
    KnowledgePermission,
)
from app.core.generation import get_answer_service
from app.core.generation.llm_client import LLMError
from app.core.retrieval.request_context import resolve_search_execution
from app.core.retrieval.search import SearchUnavailableError
from app.schemas.answer import AnswerRequest, AnswerResponse
from app.security.service_auth import require_gateway_service
from app.stores.experience_store import (
    PENETRATION_EXPERIENCE_KB_ID,
    PENETRATION_EXPERIENCE_KB_NAME,
)
from app.stores.knowledge_base_store import get_knowledge_base_store

router = APIRouter(prefix="/v1/answer", tags=["answer"])


@router.post("", response_model=AnswerResponse)
async def answer(
    request: AnswerRequest,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
) -> AnswerResponse:
    """检索知识库，并生成带可验证引用的单轮回答。"""
    knowledge_base = await get_knowledge_base_store().get(request.knowledge_base_id)
    if knowledge_base is not None and (
        knowledge_base.id == PENETRATION_EXPERIENCE_KB_ID
        or knowledge_base.name == PENETRATION_EXPERIENCE_KB_NAME
    ):
        raise HTTPException(
            status_code=403,
            detail="Experience knowledge must be queried through an authorized scope",
        )
    try:
        access_context.require(
            KnowledgePermission.ANSWER,
            knowledge_base_id=request.knowledge_base_id,
        )
    except KnowledgeAccessDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    try:
        context = await resolve_search_execution(request)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    service = get_answer_service()
    try:
        result = await service.answer(**context.search_kwargs)
    except SearchUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return AnswerResponse(**result)
