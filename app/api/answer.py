"""基于检索证据生成回答的 HTTP API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.embedding.profiles import get_embedding_profile
from app.core.generation import get_answer_service
from app.core.generation.llm_client import LLMError
from app.core.retrieval.search import SearchUnavailableError
from app.schemas.answer import AnswerRequest, AnswerResponse
from app.stores.knowledge_base_store import get_knowledge_base_store

router = APIRouter(prefix="/v1/answer", tags=["answer"])


@router.post("", response_model=AnswerResponse)
async def answer(request: AnswerRequest) -> AnswerResponse:
    """检索知识库，并生成带可验证引用的单轮回答。"""
    if not request.enable_vector and not request.enable_keyword:
        raise HTTPException(
            status_code=400,
            detail="At least one of enable_vector/enable_keyword must be True",
        )

    try:
        knowledge_base = await get_knowledge_base_store().resolve(
            request.knowledge_base_id
        )
        profile = (
            get_embedding_profile(knowledge_base.embedding_profile)
            if request.enable_vector
            else None
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    filters = (
        request.filters.model_dump(exclude_none=True)
        if request.filters is not None
        else {}
    )
    filters["knowledge_base_id"] = knowledge_base.id

    service = get_answer_service()
    try:
        result = await service.answer(
            query=request.query,
            knowledge_base_id=knowledge_base.id,
            top_k=request.top_k,
            vector_top_k=request.vector_top_k,
            keyword_top_k=request.keyword_top_k,
            max_chunks_per_document=request.max_chunks_per_document,
            fusion_method=request.fusion_method,
            vector_weight=request.vector_weight,
            keyword_weight=request.keyword_weight,
            enable_rerank=request.enable_rerank,
            enable_vector=request.enable_vector,
            enable_keyword=request.enable_keyword,
            filters=filters,
            embedding_profile=knowledge_base.embedding_profile,
            enable_abstention=request.enable_abstention,
            min_vector_score=(
                request.min_vector_score
                if request.min_vector_score is not None
                else profile.retrieval_min_score if profile is not None else None
            ),
            require_exact_entity_match=request.require_exact_entity_match,
            component_max_retries=request.component_max_retries,
        )
    except SearchUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return AnswerResponse(**result)
