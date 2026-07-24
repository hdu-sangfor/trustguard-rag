"""知识库 API 数据结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    embedding_profile: str = Field(default="configured", min_length=1, max_length=64)


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    embedding_profile: str
    embedding_provider: str
    embedding_api_driver: str | None = None
    embedding_model: str
    embedding_dim: int
    is_default: bool = False
    is_system: bool = False
    document_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]
    total: int
