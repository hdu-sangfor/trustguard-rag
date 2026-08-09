"""Experience write/feedback/admin service-token authentication."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Callable

from fastapi import Header, HTTPException, Request

from app.settings import get_settings


class ExperiencePermission(StrEnum):
    WRITE = "rag.experience.write"
    FEEDBACK = "rag.experience.feedback"
    ADMIN = "rag.experience.admin"


@dataclass(frozen=True)
class ExperienceAccessContext:
    service_id: str
    permissions: frozenset[ExperiencePermission]
    token_fingerprint: str


def _parse_tokens(raw: str) -> dict[str, frozenset[ExperiencePermission]]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise ValueError("RAG_EXPERIENCE_TOKENS_JSON must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("RAG_EXPERIENCE_TOKENS_JSON must be a JSON object")
    mapping: dict[str, frozenset[ExperiencePermission]] = {}
    for token, scopes in payload.items():
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Experience token keys must be non-empty strings")
        if not isinstance(scopes, list) or not scopes:
            raise ValueError(f"Experience token {token!r} must map to a non-empty scope list")
        perms: set[ExperiencePermission] = set()
        for scope in scopes:
            try:
                perms.add(ExperiencePermission(str(scope)))
            except ValueError as error:
                raise ValueError(f"Unsupported experience scope: {scope}") from error
        mapping[token] = frozenset(perms)
    return mapping


def load_experience_token_map(
    raw: str | None = None,
) -> dict[str, frozenset[ExperiencePermission]]:
    settings = get_settings()
    return _parse_tokens(raw if raw is not None else settings.experience_tokens_json)


def _authenticate(
    request: Request,
    authorization: str | None,
    *,
    required: ExperiencePermission | None,
    any_of: frozenset[ExperiencePermission] | None = None,
) -> ExperienceAccessContext:
    settings = get_settings()
    if not settings.experience_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXPERIENCE_DISABLED",
                "message": "Experience APIs are disabled",
                "retryable": False,
            },
        )
    if not settings.experience_auth_enabled:
        context = ExperienceAccessContext(
            service_id="development-experience",
            permissions=frozenset(ExperiencePermission),
            token_fingerprint="dev",
        )
        request.state.experience_access_context = context
        return context

    try:
        token_map = load_experience_token_map()
    except ValueError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXPERIENCE_AUTH_MISCONFIGURED",
                "message": str(error),
                "retryable": False,
            },
        ) from error
    if not token_map:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "EXPERIENCE_AUTH_MISCONFIGURED",
                "message": "Experience authentication is enabled but no tokens are configured",
                "retryable": False,
            },
        )

    scheme, separator, credential = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not credential:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "Bearer experience token required",
                "retryable": False,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    matched: frozenset[ExperiencePermission] | None = None
    for token, scopes in token_map.items():
        if secrets.compare_digest(credential, token):
            matched = scopes
            break
    if matched is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "Invalid experience credentials",
                "retryable": False,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    if required is not None and required not in matched:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "EXPERIENCE_FORBIDDEN",
                "message": f"Missing required scope {required.value}",
                "retryable": False,
            },
        )
    if any_of is not None and matched.isdisjoint(any_of):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "EXPERIENCE_FORBIDDEN",
                "message": "Token lacks any experience scope",
                "retryable": False,
            },
        )

    fingerprint = (
        credential[:4] + "…" + credential[-4:] if len(credential) > 8 else "token"
    )
    context = ExperienceAccessContext(
        service_id=f"experience-token:{fingerprint}",
        permissions=matched,
        token_fingerprint=fingerprint,
    )
    request.state.experience_access_context = context
    return context


def require_experience_permission(
    permission: ExperiencePermission,
) -> Callable[..., ExperienceAccessContext]:
    async def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ExperienceAccessContext:
        return _authenticate(request, authorization, required=permission)

    return dependency


def require_any_experience_permission() -> Callable[..., ExperienceAccessContext]:
    async def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> ExperienceAccessContext:
        return _authenticate(
            request,
            authorization,
            required=None,
            any_of=frozenset(ExperiencePermission),
        )

    return dependency
