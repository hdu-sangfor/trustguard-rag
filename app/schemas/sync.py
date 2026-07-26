"""增量同步请求/响应 schema。

对照 LangChain index() 的 num_added/updated/skipped/deleted 与 cleanup 语义。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SyncItem(BaseModel):
    source_uri: str = Field(..., min_length=1, max_length=2048)
    filename: str = Field(..., min_length=1, max_length=512)
    content_b64: str = Field(..., min_length=1, description="Base64-encoded file bytes")
    mime: str | None = None
    collected_at: str | None = None


class SyncDirectory(BaseModel):
    root_key: str = Field(..., min_length=1, max_length=64)
    relative_path: str = Field(default=".", max_length=1024)
    glob: str = Field(default="**/*.{md,txt,html,htm,json,csv}", max_length=256)
    source_uri_template: str = Field(
        default="crawler://{root_key}/{relative}",
        max_length=512,
        description="用 {root_key}/{relative} 生成稳定 source_uri",
    )


class SyncRequest(BaseModel):
    knowledge_base_id: str | None = None
    conflict_policy: Literal["manual", "keep_new"] = "keep_new"
    cleanup: Literal["none", "full"] = "none"
    source_uri_prefix: str | None = Field(
        default=None,
        max_length=2048,
        description="cleanup=full 时必填；用于限定对账范围",
    )
    cursor_key: str | None = Field(default=None, max_length=64)
    items: list[SyncItem] | None = None
    directory: SyncDirectory | None = None
    wait: bool = True
    wait_timeout_sec: float = Field(default=300.0, ge=1.0, le=3600.0)

    @model_validator(mode="after")
    def require_items_or_directory(self) -> SyncRequest:
        has_items = bool(self.items)
        has_dir = self.directory is not None
        if has_items == has_dir:
            raise ValueError("Provide exactly one of items or directory")
        if self.cleanup == "full" and not (self.source_uri_prefix or "").strip():
            raise ValueError("source_uri_prefix is required when cleanup=full")
        return self


class SyncResponse(BaseModel):
    sync_id: str
    knowledge_base_id: str
    added: int = 0
    skipped: int = 0
    updated: int = 0
    failed: int = 0
    deleted: int = 0
    job_ids: list[str] = Field(default_factory=list)
    cursor_key: str | None = None
    cursor_value: str | None = None
    errors: list[str] = Field(default_factory=list)


class CursorResponse(BaseModel):
    cursor_key: str
    cursor_value: str | None
