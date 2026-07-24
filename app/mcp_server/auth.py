"""MCP Bearer JWT 验证与逻辑知识 Scope 授权。"""

from __future__ import annotations

import asyncio
import time
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


def authorize_knowledge_scope(
    scope: str,
    *,
    required_permission: str,
    auth_enabled: bool,
) -> None:
    if not auth_enabled:
        return
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


def _token_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return list(dict.fromkeys(value.split()))
    if isinstance(value, list):
        return list(
            dict.fromkeys(str(item) for item in value if isinstance(item, str))
        )
    return []
