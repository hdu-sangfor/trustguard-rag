"""Crawler 管理 API 数据结构。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.crawler import CrawlJobStatus


class CrawlJobCreateRequest(BaseModel):
    knowledge_base_id: str
    preset_ids: list[str] = Field(default_factory=list, max_length=10)
    urls: list[str] = Field(default_factory=list, max_length=200)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    site_urls: list[str] = Field(default_factory=list, max_length=50)
    structured_sources: list[str] = Field(default_factory=list, max_length=10)
    source_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_results_per_keyword: int | None = Field(default=None, ge=1, le=20)
    max_pages_per_site: int | None = Field(default=None, ge=1, le=50)
    max_total_pages: int | None = Field(default=None, ge=1, le=200)
    max_chars: int | None = Field(default=None, ge=0, le=1_000_000)
    min_content_chars: int | None = Field(default=None, ge=0, le=10_000)
    timeout_seconds: float | None = Field(default=None, ge=1.0, le=120.0)
    fetch_delay_seconds: float | None = Field(default=None, ge=0.0, le=30.0)
    max_retries: int | None = Field(default=None, ge=0, le=10)
    retry_base_seconds: float | None = Field(default=None, ge=0.0, le=60.0)
    route_by_category: bool = False
    force: bool = False
    require_review: bool = False
    review_mode: Literal["human", "agent"] = "human"
    review_criteria: str = Field(default="", max_length=8_000)

    @model_validator(mode="after")
    def require_source(self) -> "CrawlJobCreateRequest":
        if not any(
            (
                self.preset_ids,
                self.urls,
                self.keywords,
                self.site_urls,
                self.structured_sources,
            )
        ):
            raise ValueError("At least one web or structured source is required")
        if len(self.source_options) > 10:
            raise ValueError("At most 10 structured source option groups are allowed")
        for source_id, options in self.source_options.items():
            if len(source_id) > 64:
                raise ValueError("Structured source ID is too long")
            if "limit" in options:
                limit = options["limit"]
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
                    raise ValueError("Structured source limit must be an integer from 1 to 200")
            if "ids" in options:
                ids = options["ids"]
                if not isinstance(ids, list) or len(ids) > 200:
                    raise ValueError("Structured source IDs must be a list of at most 200 items")
            if "offset" in options:
                offset = options["offset"]
                if (
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or offset < 0
                ):
                    raise ValueError(
                        "Structured source offset must be a non-negative integer"
                    )
            if "category" in options:
                category = options["category"]
                if not isinstance(category, str) or len(category) > 128:
                    raise ValueError(
                        "Structured source category must be a string up to 128 characters"
                    )
        if self.review_mode == "agent":
            if not self.require_review:
                raise ValueError("Agent review requires require_review=true")
            if not self.review_criteria.strip() and not self.preset_ids:
                raise ValueError("Agent review criteria are required")
        return self


class CrawlJobResponse(BaseModel):
    id: str
    knowledge_base_id: str
    status: CrawlJobStatus
    config: dict[str, Any]
    progress: dict[str, Any]
    ingest_job_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None
    attempt: int = 0
    cancel_requested: bool = False
    pause_requested: bool = False
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class CrawlJobListResponse(BaseModel):
    items: list[CrawlJobResponse]
    total: int


class CrawlerDefaultsResponse(BaseModel):
    max_results_per_keyword: int
    max_pages_per_site: int
    max_total_pages: int
    max_chars: int
    min_content_chars: int
    timeout_seconds: float
    fetch_delay_seconds: float
    max_retries: int
    retry_base_seconds: float
    allow_private_urls: bool
    agent_review_available: bool
    agent_review_model: str | None = None


class StructuredSourceResponse(BaseModel):
    id: str
    name: str
    description: str
    mode: str
    default_limit: int


class StructuredSourceListResponse(BaseModel):
    items: list[StructuredSourceResponse]


class LegacyCorpusCategoryResponse(BaseModel):
    name: str
    document_count: int


class LegacyCorpusCatalogResponse(BaseModel):
    available: bool
    items: list[LegacyCorpusCategoryResponse] = Field(default_factory=list)
    total_documents: int = 0
    error: str | None = None


class CrawlerPresetResponse(BaseModel):
    id: str
    name: str
    description: str
    kind: str = "source"
    category_name: str | None = None
    site_urls: list[str]
    keywords: list[str]
    structured_sources: list[str] = Field(default_factory=list)
    domain_category: str | None = None
    kb_tier: str | None = None
    phases: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    priority: str | None = None
    review_criteria: str = ""


class CrawlerPresetListResponse(BaseModel):
    items: list[CrawlerPresetResponse]


class CrawlReviewActionRequest(BaseModel):
    action: Literal["approve", "reject"]
    item_ids: list[str] = Field(min_length=1, max_length=200)


class CrawlReviewItemResponse(BaseModel):
    id: str
    status: str
    knowledge_base_id: str
    title: str
    source_uri: str
    source_type: str
    original_filename: str
    content_preview: str
    content_chars: int
    created_at: str
    ingest_job_id: str | None = None
    reviewer: str | None = None
    review_reason: str | None = None
    review_confidence: float | None = None


class CrawlReviewResponse(BaseModel):
    job_id: str
    review_status: str
    review_mode: str = "human"
    review_criteria: str = ""
    items: list[CrawlReviewItemResponse]
    pending: int
    approved: int
    rejected: int


class CrawlReviewContentResponse(BaseModel):
    item: CrawlReviewItemResponse
    content: str
