"""MCP Bearer JWT 验证与逻辑知识 Scope 授权。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken


class JwtTokenVerifier:
    """通过远端 JWKS 验证短期 Access Token，不持有签名私钥。"""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: list[str],
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms
        self._jwks = jwt.PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=300,
            cache_keys=True,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await asyncio.to_thread(
                self._jwks.get_signing_key_from_jwt,
                token,
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            scopes = _token_scopes(claims.get("scope"))
            subject = str(claims["sub"])
            expires_at = int(claims["exp"])
            if expires_at <= int(time.time()):
                return None
            return AccessToken(
                token=token,
                client_id=subject,
                subject=subject,
                scopes=scopes,
                expires_at=expires_at,
                resource=self._audience,
                claims=claims,
            )
        except (
            jwt.InvalidTokenError,
            jwt.PyJWKClientError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return None


class ScopeAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class AuthorizedKnowledgeContext:
    workspace_id: str
    allowed_workflow_types: frozenset[str] | None


def authorize_knowledge_scope(
    scope: str,
    *,
    required_permission: str,
    auth_enabled: bool,
    default_workspace_id: str = "default",
) -> AuthorizedKnowledgeContext:
    if not auth_enabled:
        return AuthorizedKnowledgeContext(
            workspace_id=default_workspace_id,
            allowed_workflow_types=None,
        )
    access_token = get_access_token()
    if access_token is None:
        raise ScopeAuthorizationError("Authentication is required")
    if required_permission not in access_token.scopes:
        raise ScopeAuthorizationError("The access token lacks the required permission")
    claims: dict[str, Any] = access_token.claims or {}
    allowed = claims.get("knowledge_scopes")
    if not isinstance(allowed, list) or scope not in {
        str(item) for item in allowed if isinstance(item, str)
    }:
        raise ScopeAuthorizationError(
            "The access token is not authorized for this knowledge scope"
        )
    workspace_id = claims.get("workspace_id", default_workspace_id)
    if not isinstance(workspace_id, str) or workspace_id != default_workspace_id:
        raise ScopeAuthorizationError(
            "The access token is not authorized for this workspace"
        )
    raw_workflow_types = claims.get("workflow_types")
    if raw_workflow_types is None:
        allowed_workflow_types = frozenset()
    elif isinstance(raw_workflow_types, list):
        allowed_workflow_types = frozenset(
            str(item) for item in raw_workflow_types if isinstance(item, str)
        )
    else:
        raise ScopeAuthorizationError("The workflow_types claim is invalid")
    return AuthorizedKnowledgeContext(
        workspace_id=workspace_id,
        allowed_workflow_types=allowed_workflow_types,
    )


def _token_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return list(dict.fromkeys(value.split()))
    if isinstance(value, list):
        return list(
            dict.fromkeys(str(item) for item in value if isinstance(item, str))
        )
    return []
