"""稳定错误信封与请求 ID 传播。"""

from __future__ import annotations

import re
import logging
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.api import StableErrorResponse

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
logger = logging.getLogger(__name__)


def resolve_request_id(value: str | None) -> str:
    """只接受可安全记录和回传的请求 ID，否则生成服务端 ID。"""
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return f"req-{uuid4()}"


def _code_for_status(status_code: int) -> str:
    return {
        401: "AUTH_REQUIRED",
        403: "AUTH_FORBIDDEN",
        404: "RESOURCE_NOT_FOUND",
        408: "RAG_TIMEOUT",
        422: "INVALID_ARGUMENT",
        429: "RATE_LIMITED",
        503: "RAG_UNAVAILABLE",
        504: "RAG_TIMEOUT",
    }.get(
        status_code,
        "INVALID_ARGUMENT" if 400 <= status_code < 500 else "INTERNAL_ERROR",
    )


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    detail: Any,
    retryable: bool,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", resolve_request_id(None))
    envelope = StableErrorResponse(
        request_id=request_id,
        code=code,
        message=message,
        retryable=retryable,
        detail=detail,
    )
    response = JSONResponse(
        status_code=status_code,
        headers=headers,
        # detail 保留 FastAPI 历史语义，避免破坏现有客户端。
        content=jsonable_encoder(envelope.model_dump()),
    )
    response.headers["X-Request-ID"] = request_id
    return response


async def http_exception_handler(request: Request, error: HTTPException) -> JSONResponse:
    detail = error.detail
    message = detail if isinstance(detail, str) else "Request failed"
    return _response(
        request,
        status_code=error.status_code,
        code=_code_for_status(error.status_code),
        message=message,
        detail=detail,
        retryable=error.status_code in {408, 429, 503, 504},
        headers=error.headers,
    )


async def validation_exception_handler(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    return _response(
        request,
        status_code=422,
        code="INVALID_ARGUMENT",
        message="Request validation failed",
        detail=error.errors(),
        retryable=False,
    )


async def unhandled_exception_handler(request: Request, error: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(
        "unhandled API error request_id=%s",
        request_id,
        exc_info=(type(error), error, error.__traceback__),
    )
    return _response(
        request,
        status_code=500,
        code="INTERNAL_ERROR",
        message="Internal server error",
        detail=None,
        retryable=False,
    )
