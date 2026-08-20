"""Crawler 管理 API 数据结构。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.domain.crawler import CrawlJobStatus


class CrawlJobCreateRequest(BaseModel):
    knowledge_base_id: str
    preset_ids: list[str] = Field(default_factory=list, max_length=10)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    urls: list[str] = Field(default_factory=list, max_length=200)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    site_urls: list[str] = Field(default_factory=list, max_length=50)
    rss_urls: list[str] = Field(default_factory=list, max_length=50)
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
                self.rss_urls,
                self.structured_sources,
                self.source_ids,
            )
        ):
            raise ValueError("At least one web or structured source is required")
        if len(self.source_ids) > 1:
            raise ValueError("At most one managed crawler source is allowed per job")
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


CrawlerSourceKind = Literal["url", "site", "rss", "structured", "preset", "custom"]


def validate_crawler_source_config(
    *,
    source_kind: CrawlerSourceKind,
    endpoint: str | None,
    preset_ids: list[str],
    config: dict[str, Any],
) -> None:
    """Apply one-shot crawler limits to the persisted source configuration."""
    candidate = dict(config)

    def append_values(key: str, values: list[str]) -> None:
        current = candidate.get(key)
        if current is None:
            candidate[key] = values
        elif isinstance(current, list):
            candidate[key] = [*current, *values]

    if candidate.get("source_ids"):
        raise ValueError("Managed source config cannot reference another source")
    candidate["knowledge_base_id"] = "crawler-source-validation"
    if source_kind == "url" and endpoint:
        append_values("urls", [endpoint])
    elif source_kind == "site" and endpoint:
        append_values("site_urls", [endpoint])
    elif source_kind == "rss" and endpoint:
        append_values("rss_urls", [endpoint])
    elif source_kind == "structured" and endpoint:
        append_values("structured_sources", [endpoint])
    elif source_kind == "preset":
        append_values("preset_ids", preset_ids)
    try:
        validated = CrawlJobCreateRequest.model_validate(candidate)
    except ValidationError as error:
        messages = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        raise ValueError(f"Invalid crawler source config: {messages}") from error
    if source_kind == "preset":
        from app.core.crawler.presets import expand_crawler_presets

        try:
            expand_crawler_presets(validated.preset_ids)
        except ValueError as error:
            raise ValueError(f"Invalid crawler source config: {error}") from error
    if validated.structured_sources:
        from app.core.crawler.structured import default_structured_registry

        known_sources = {item.id for item in default_structured_registry().infos()}
        unknown_sources = set(validated.structured_sources) - known_sources
        if unknown_sources:
            unknown = ", ".join(sorted(unknown_sources))
            raise ValueError(
                f"Invalid crawler source config: unknown structured sources: {unknown}"
            )


class CrawlerSourceCreateRequest(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=64)
    knowledge_base_id: str
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    source_kind: CrawlerSourceKind
    endpoint: str | None = Field(default=None, max_length=2048)
    preset_ids: list[str] = Field(default_factory=list, max_length=20)
    config: dict[str, Any] = Field(default_factory=dict)
    trust_level: Literal["official", "trusted", "community", "unverified"] = "trusted"
    content_type: str = Field(default="security_knowledge", min_length=1, max_length=64)
    usage_restrictions: str | None = Field(default=None, max_length=4000)
    enabled: bool = True
    schedule_enabled: bool = False
    schedule_interval_minutes: int | None = Field(default=None, ge=5, le=525_600)
    next_run_at: datetime | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "CrawlerSourceCreateRequest":
        if self.source_kind in {"url", "site", "rss", "structured"} and not (
            self.endpoint or ""
        ).strip():
            raise ValueError(f"{self.source_kind} source requires endpoint")
        if self.schedule_enabled and self.schedule_interval_minutes is None:
            raise ValueError("Scheduled source requires schedule_interval_minutes")
        if self.source_kind == "preset" and not self.preset_ids:
            raise ValueError("Preset source requires preset_ids")
        validate_crawler_source_config(
            source_kind=self.source_kind,
            endpoint=self.endpoint,
            preset_ids=self.preset_ids,
            config=self.config,
        )
        return self


class CrawlerSourceUpdateRequest(BaseModel):
    knowledge_base_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)
    source_kind: CrawlerSourceKind | None = None
    endpoint: str | None = Field(default=None, max_length=2048)
    preset_ids: list[str] | None = Field(default=None, max_length=20)
    config: dict[str, Any] | None = None
    trust_level: Literal["official", "trusted", "community", "unverified"] | None = None
    content_type: str | None = Field(default=None, min_length=1, max_length=64)
    usage_restrictions: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None
    schedule_enabled: bool | None = None
    schedule_interval_minutes: int | None = Field(default=None, ge=5, le=525_600)
    next_run_at: datetime | None = None


class CrawlerSourceStatsResponse(BaseModel):
    run_count: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    success_rate: float | None = None
    fetched: int = 0
    duplicates: int = 0
    not_modified: int = 0
    failed_items: int = 0
    duplicate_rate: float | None = None
    approved: int = 0
    rejected: int = 0
    approval_rate: float | None = None
    resource_count: int = 0
    active_versions: int = 0
    freshness_seconds: int | None = None
    freshness_status: Literal["never", "fresh", "stale"] = "never"


class CrawlerSourceResponse(BaseModel):
    id: str
    knowledge_base_id: str
    name: str
    description: str | None = None
    source_kind: str
    endpoint: str | None = None
    preset_ids: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    trust_level: str
    content_type: str
    usage_restrictions: str | None = None
    enabled: bool
    schedule_enabled: bool
    schedule_interval_minutes: int | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_job_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    stats: CrawlerSourceStatsResponse | None = None


class CrawlerSourceListResponse(BaseModel):
    items: list[CrawlerSourceResponse]
    total: int


class CrawlerSourceRunRequest(BaseModel):
    require_review: bool | None = None
    review_mode: Literal["human", "agent"] | None = None
    review_criteria: str | None = Field(default=None, max_length=8000)
    force: bool = False


class CrawlerSourceVersionResponse(BaseModel):
    id: str
    source_id: str
    resource_url: str
    crawl_job_id: str
    ingest_job_id: str | None = None
    document_id: str | None = None
    content_hash: str
    version: int
    status: str
    supersedes_version_id: str | None = None
    created_at: datetime | None = None
    activated_at: datetime | None = None
    superseded_at: datetime | None = None


class CrawlerSourceVersionListResponse(BaseModel):
    items: list[CrawlerSourceVersionResponse]
    total: int


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
    reviewer: str | None = Field(default=None, min_length=1, max_length=128)


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
    agent_decision: str | None = None
    manual_reviewer: str | None = None
    manual_reviewed_at: str | None = None
    rejected_at: str | None = None
    review_content_expires_at: str | None = None
    review_content_expired_at: str | None = None
    review_content_available: bool = True


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
