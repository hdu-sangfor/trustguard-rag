"""Experience authority REST API (Slice A)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.application.experience import ExperienceError, get_experience_service
from app.domain.experience import ExperienceStatus
from app.schemas.experience import (
    ExperienceFeedbackRequest,
    ExperienceFeedbackResponse,
    ExperienceListResponse,
    ExperienceResponse,
    ExperienceStatusPatchRequest,
    ExperienceUpsertRequest,
)
from app.application.access import KnowledgeAccessContext
from app.security.service_auth import require_gateway_service
from app.settings import get_settings

router = APIRouter(prefix="/v1/experiences", tags=["experiences"])


def _raise(error: ExperienceError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        },
    ) from error


def _ensure_enabled() -> None:
    if not get_settings().experience_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXPERIENCE_DISABLED",
                "message": "Experience APIs are disabled",
                "retryable": False,
            },
        )


@router.put("/{external_id}", response_model=ExperienceResponse)
async def upsert_experience(
    external_id: str,
    body: ExperienceUpsertRequest,
    access: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> ExperienceResponse:
    _ensure_enabled()
    try:
        return await get_experience_service().upsert(
            external_id,
            body,
            access=access,
            idempotency_key=idempotency_key,
        )
    except ExperienceError as error:
        _raise(error)
        raise  # pragma: no cover


@router.get("/{experience_id}", response_model=ExperienceResponse)
async def get_experience(
    experience_id: str,
    access: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
) -> ExperienceResponse:
    _ensure_enabled()
    _ = access
    try:
        return await get_experience_service().get(experience_id, access=access)
    except ExperienceError as error:
        _raise(error)
        raise  # pragma: no cover


@router.get("", response_model=ExperienceListResponse)
async def list_experiences(
    access: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
    status: Annotated[ExperienceStatus | None, Query()] = None,
    workflow_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExperienceListResponse:
    _ensure_enabled()
    _ = access
    return await get_experience_service().list_experiences(
        access=access,
        status=status.value if status else None,
        workflow_type=workflow_type,
        limit=limit,
        offset=offset,
    )


@router.patch("/{experience_id}/status", response_model=ExperienceResponse)
async def patch_experience_status(
    experience_id: str,
    body: ExperienceStatusPatchRequest,
    access: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
) -> ExperienceResponse:
    _ensure_enabled()
    try:
        return await get_experience_service().patch_status(
            experience_id, body, access=access
        )
    except ExperienceError as error:
        _raise(error)
        raise  # pragma: no cover


@router.post("/{experience_id}/feedback", response_model=ExperienceFeedbackResponse)
async def post_experience_feedback(
    experience_id: str,
    body: ExperienceFeedbackRequest,
    access: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=128),
    ] = None,
) -> ExperienceFeedbackResponse:
    _ensure_enabled()
    try:
        return await get_experience_service().feedback(
            experience_id,
            body,
            access=access,
            idempotency_key=idempotency_key,
        )
    except ExperienceError as error:
        _raise(error)
        raise  # pragma: no cover
