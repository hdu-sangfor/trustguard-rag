"""基于检索证据生成回答的 HTTP API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.generation import get_answer_service
from app.core.generation.llm_client import LLMError
from app.core.retrieval.search import SearchUnavailableError
from app.schemas.answer import AnswerRequest, AnswerResponse

router = APIRouter(prefix="/v1/answer", tags=["answer"])


@router.post("", response_model=AnswerResponse)
async def answer(request: AnswerRequest) -> AnswerResponse:
    """检索知识库，并生成带可验证引用的单轮回答。"""
    if not request.enable_vector and not request.enable_keyword:
        raise HTTPException(
            status_code=400,
            detail="At least one of enable_vector/enable_keyword must be True",
        )

    service = get_answer_service()
    try:
        result = await service.answer(
            query=request.query,
            top_k=request.top_k,
            vector_top_k=request.vector_top_k,
            keyword_top_k=request.keyword_top_k,
            fusion_method=request.fusion_method,
            vector_weight=request.vector_weight,
            keyword_weight=request.keyword_weight,
            enable_rerank=request.enable_rerank,
            enable_vector=request.enable_vector,
            enable_keyword=request.enable_keyword,
            filters=(
                request.filters.model_dump(exclude_none=True)
                if request.filters is not None
                else None
            ),
        )
    except SearchUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return AnswerResponse(**result)
