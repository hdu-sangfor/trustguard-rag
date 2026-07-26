"""不可解析的知识 Resource Ref 签发与校验。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "krf1."
_AAD = b"trustguard-rag-resource-ref-v1"


class InvalidResourceRef(ValueError):
    """Resource Ref 格式错误、被篡改或无法解密。"""


@dataclass(frozen=True)
class ResourceRefClaims:
    scope: str
    knowledge_base_id: str
    chunk_id: str
    source_revision: int
    content_hash: str


class ResourceRefCodec:
    """使用 AES-GCM 隐藏并认证物理来源身份。"""

    def __init__(self, secret: str) -> None:
        normalized = secret.strip()
        if not normalized:
            raise ValueError("Resource Ref secret cannot be empty")
        self._cipher = AESGCM(hashlib.sha256(normalized.encode("utf-8")).digest())

    def issue(self, claims: ResourceRefClaims) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "s": claims.scope,
                "k": claims.knowledge_base_id,
                "c": claims.chunk_id,
                "r": claims.source_revision,
                "h": claims.content_hash,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = os.urandom(12)
        encrypted = self._cipher.encrypt(nonce, payload, _AAD)
        token = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")
        return f"{_PREFIX}{token}"

    def parse(self, resource_ref: str) -> ResourceRefClaims:
        if not resource_ref.startswith(_PREFIX):
            raise InvalidResourceRef("Unsupported Resource Ref version")
        encoded = resource_ref.removeprefix(_PREFIX)
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(raw) <= 28:
                raise ValueError("Resource Ref payload is too short")
            payload = self._cipher.decrypt(raw[:12], raw[12:], _AAD)
            value: Any = json.loads(payload)
            if not isinstance(value, dict) or value.get("v") != 1:
                raise ValueError("Resource Ref payload is invalid")
            claims = ResourceRefClaims(
                scope=_required_string(value, "s", max_length=64),
                knowledge_base_id=_required_string(value, "k", max_length=128),
                chunk_id=_required_string(value, "c", max_length=128),
                source_revision=_required_revision(value.get("r")),
                content_hash=_required_content_hash(value.get("h")),
            )
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError) as error:
            raise InvalidResourceRef("Resource Ref is invalid") from error
        return claims


def _required_string(value: dict[str, Any], key: str, *, max_length: int) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not 1 <= len(result) <= max_length:
        raise ValueError(f"Resource Ref field {key} is invalid")
    return result


def _required_revision(value: Any) -> int:
    if not isinstance(value, int) or value < 1:
        raise ValueError("Resource Ref source revision is invalid")
    return value


def _required_content_hash(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Resource Ref content hash is invalid")
    normalized = value.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("Resource Ref content hash is invalid")
    return normalized
