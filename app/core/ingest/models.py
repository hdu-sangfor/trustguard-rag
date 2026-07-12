"""抽取器输出模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedDocument:
    text: str
    content_hash: str
    source_uri: str
    mime: str
    raw_bytes: bytes
    raw_filename: str
    metadata: dict[str, Any] = field(default_factory=dict)
