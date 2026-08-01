"""稳定 source_uri 校验与覆盖。

对照 ragversion 变更检测：逻辑文档身份由稳定 URI 标识，内容变更由 content_hash 判定。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.core.ingest.models import ExtractedDocument

_MAX_SOURCE_URI_LEN = 2048
_ALLOWED_SCHEMES = frozenset(
    {
        "http",
        "https",
        "crawler",
        "legacy-corpus",
        "file",
        "upload",
        "sync",
        "nvd",
        "cve",
        "attack",
        "kev",
    }
)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


class SourceUriError(ValueError):
    """source_uri 不合法。"""


def validate_source_uri(source_uri: str) -> str:
    """校验并规范化稳定 source_uri。"""
    uri = (source_uri or "").strip()
    if not uri:
        raise SourceUriError("source_uri must not be empty")
    if len(uri) > _MAX_SOURCE_URI_LEN:
        raise SourceUriError(f"source_uri exceeds {_MAX_SOURCE_URI_LEN} characters")
    if ".." in uri.replace("\\", "/").split("/"):
        raise SourceUriError("source_uri must not contain path traversal segments")
    if not _SCHEME_RE.match(uri):
        raise SourceUriError("source_uri must include a scheme (e.g. crawler://...)")
    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SourceUriError(
            f"source_uri scheme '{scheme}' is not allowed; "
            f"allowed={sorted(_ALLOWED_SCHEMES)}"
        )
    return uri


def apply_source_overrides(
    extracted: ExtractedDocument,
    options: dict[str, Any] | None,
) -> ExtractedDocument:
    """用任务 options 覆盖稳定 URI，并写入采集时间等元数据。"""
    opts = options or {}
    override = opts.get("source_uri")
    if override:
        extracted.source_uri = validate_source_uri(str(override))
    collected_at = opts.get("collected_at")
    if collected_at:
        meta = dict(extracted.metadata or {})
        meta["collected_at"] = str(collected_at)
        extracted.metadata = meta
    return extracted


def normalize_conflict_policy(value: str | None) -> str:
    """返回 manual | keep_new。"""
    policy = (value or "manual").strip().lower()
    if policy not in {"manual", "keep_new"}:
        raise ValueError("conflict_policy must be 'manual' or 'keep_new'")
    return policy
