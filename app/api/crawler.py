"""Crawler 任务管理 API。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query, status

from app.core.crawler.structured import default_structured_registry
from app.core.crawler.presets import CRAWLER_PRESETS, expand_crawler_presets
from app.core.crawler.review import apply_review, get_review, get_review_content
from app.schemas.crawler import (
    CrawlerPresetListResponse,
    CrawlerPresetResponse,
    CrawlerDefaultsResponse,
    CrawlJobCreateRequest,
    CrawlJobListResponse,
    CrawlJobResponse,
    CrawlReviewActionRequest,
    CrawlReviewContentResponse,
    CrawlReviewResponse,
    LegacyCorpusCatalogResponse,
    LegacyCorpusCategoryResponse,
    StructuredSourceListResponse,
    StructuredSourceResponse,
)
from app.settings import get_settings
from app.stores.crawler_store import CrawlerStore
from app.workers.eager import dispatch_eager

router = APIRouter(prefix="/v1/crawler", tags=["crawler"])


def _response(row) -> CrawlJobResponse:
    return CrawlJobResponse(
        id=row.id,
        knowledge_base_id=row.knowledge_base_id,
        status=row.status,
        config=dict(row.config_json or {}),
        progress=dict(row.progress_json or {}),
        ingest_job_ids=list(row.ingest_job_ids_json or []),
        error_message=row.error_message,
        attempt=row.attempt or 0,
        cancel_requested=bool(row.cancel_requested),
        pause_requested=bool(row.pause_requested),
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        updated_at=row.updated_at,
    )


@router.get("/presets", response_model=CrawlerPresetListResponse)
async def crawler_presets() -> CrawlerPresetListResponse:
    def response_item(preset) -> CrawlerPresetResponse:
        expanded = expand_crawler_presets([preset.id]) if preset.kind == "category" else None
        return CrawlerPresetResponse(
            id=preset.id,
            name=preset.name,
            description=preset.description,
            kind=preset.kind,
            category_name=preset.category_name,
            site_urls=list(expanded.site_urls if expanded else preset.site_urls),
            keywords=list(expanded.keywords if expanded else preset.keywords),
            structured_sources=list(
                expanded.structured_sources if expanded else preset.structured_sources
            ),
            domain_category=preset.domain_category,
            kb_tier=preset.kb_tier,
            phases=list(preset.phases),
            topic_tags=list(preset.topic_tags),
            priority=preset.priority,
            review_criteria=preset.review_criteria,
        )

    return CrawlerPresetListResponse(
        items=[response_item(preset) for preset in CRAWLER_PRESETS]
    )


@router.get("/sources", response_model=StructuredSourceListResponse)
async def structured_sources() -> StructuredSourceListResponse:
    return StructuredSourceListResponse(
        items=[
            StructuredSourceResponse(**asdict(info))
            for info in default_structured_registry().infos()
        ]
    )


@router.get("/legacy-corpus", response_model=LegacyCorpusCatalogResponse)
async def legacy_corpus_catalog() -> LegacyCorpusCatalogResponse:
    adapter = default_structured_registry().get("legacy_corpus")
    try:
        categories = await asyncio.to_thread(adapter.categories)
    except ValueError as error:
        return LegacyCorpusCatalogResponse(
            available=False,
            error=str(error),
        )
    items = [
        LegacyCorpusCategoryResponse(name=name, document_count=count)
        for name, count in categories
    ]
    return LegacyCorpusCatalogResponse(
        available=True,
        items=items,
        total_documents=sum(item.document_count for item in items),
    )


@router.get("/defaults", response_model=CrawlerDefaultsResponse)
async def crawler_defaults() -> CrawlerDefaultsResponse:
    settings = get_settings()
    return CrawlerDefaultsResponse(
        max_results_per_keyword=settings.crawler_max_results_per_keyword,
        max_pages_per_site=settings.crawler_max_pages_per_site,
        max_total_pages=settings.crawler_max_total_pages,
        max_chars=settings.crawler_max_chars,
        min_content_chars=settings.crawler_min_content_chars,
        timeout_seconds=settings.crawler_timeout_seconds,
        fetch_delay_seconds=settings.crawler_fetch_delay_seconds,
        max_retries=settings.crawler_max_retries,
        retry_base_seconds=settings.crawler_retry_base_seconds,
        allow_private_urls=settings.crawler_allow_private_urls,
        agent_review_available=bool(
            settings.llm_provider.strip().lower() not in {"none", "disabled", "off"}
            and settings.llm_base_url
            and settings.llm_model.strip()
            and (settings.llm_api_key or os.getenv("DASHSCOPE_API_KEY"))
        ),
        agent_review_model=(
            settings.llm_model
            if settings.llm_provider.strip().lower() not in {"none", "disabled", "off"}
            else None
        ),
    )


@router.post(
    "/jobs",
    response_model=CrawlJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_crawl_job(body: CrawlJobCreateRequest) -> CrawlJobResponse:
    settings = get_settings()
    if not settings.crawler_enabled:
        raise HTTPException(status_code=503, detail="Crawler is disabled")
    config = body.model_dump()
    configured_defaults = {
        "max_results_per_keyword": settings.crawler_max_results_per_keyword,
        "max_pages_per_site": settings.crawler_max_pages_per_site,
        "max_total_pages": settings.crawler_max_total_pages,
        "max_chars": settings.crawler_max_chars,
        "min_content_chars": settings.crawler_min_content_chars,
        "timeout_seconds": settings.crawler_timeout_seconds,
        "fetch_delay_seconds": settings.crawler_fetch_delay_seconds,
        "max_retries": settings.crawler_max_retries,
        "retry_base_seconds": settings.crawler_retry_base_seconds,
    }
    for key, value in configured_defaults.items():
        if config.get(key) is None:
            config[key] = value
    for key in ("preset_ids", "urls", "keywords", "site_urls", "structured_sources"):
        config[key] = list(dict.fromkeys(value.strip() for value in config[key] if value.strip()))
    try:
        expanded = expand_crawler_presets(config["preset_ids"])
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    config["site_urls"] = list(
        dict.fromkeys([*config["site_urls"], *expanded.site_urls])
    )
    config["keywords"] = list(
        dict.fromkeys([*config["keywords"], *expanded.keywords])
    )
    config["structured_sources"] = list(
        dict.fromkeys(
            [*config["structured_sources"], *expanded.structured_sources]
        )
    )
    config["source_options"] = {
        source_id: {
            **expanded.source_options.get(source_id, {}),
            **dict(config["source_options"].get(source_id, {})),
        }
        for source_id in dict.fromkeys(
            [
                *expanded.source_options,
                *config["source_options"],
            ]
        )
    }
    if expanded.category_name:
        config["target_category"] = expanded.category_name
        config["route_by_category"] = True
    if expanded.domain_category:
        config["domain_category"] = expanded.domain_category
    if expanded.kb_tier:
        config["kb_tier"] = expanded.kb_tier
    if expanded.phases:
        config["agent_phases"] = expanded.phases
    if expanded.topic_tags:
        config["topic_tags"] = expanded.topic_tags
    if expanded.priority:
        config["category_priority"] = expanded.priority
    if not str(config.get("review_criteria") or "").strip() and expanded.review_criteria:
        config["review_criteria"] = expanded.review_criteria
    if config.get("review_mode") == "agent" and not str(
        config.get("review_criteria") or ""
    ).strip():
        raise HTTPException(status_code=422, detail="Agent review criteria are required")
    known_sources = {item.id for item in default_structured_registry().infos()}
    unknown_sources = set(config["structured_sources"]) - known_sources
    if unknown_sources:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown structured sources: {', '.join(sorted(unknown_sources))}",
        )
    store = CrawlerStore()
    try:
        row, event = await store.create_job(
            knowledge_base_id=body.knowledge_base_id,
            config=config,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    await dispatch_eager(event)
    return _response(await store.get(row.id) or row)


@router.get("/jobs", response_model=CrawlJobListResponse)
async def list_crawl_jobs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    knowledge_base_id: str | None = Query(default=None, max_length=36),
) -> CrawlJobListResponse:
    rows, total = await CrawlerStore().list(
        offset=offset,
        limit=limit,
        knowledge_base_id=knowledge_base_id,
    )
    return CrawlJobListResponse(items=[_response(row) for row in rows], total=total)


@router.get("/jobs/{job_id}", response_model=CrawlJobResponse)
async def get_crawl_job(job_id: str) -> CrawlJobResponse:
    row = await CrawlerStore().get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Crawler job not found")
    return _response(row)


@router.get("/jobs/{job_id}/review", response_model=CrawlReviewResponse)
async def crawler_job_review(job_id: str) -> CrawlReviewResponse:
    try:
        result = await get_review(job_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return CrawlReviewResponse(**result)


@router.get(
    "/jobs/{job_id}/review/items/{item_id}",
    response_model=CrawlReviewContentResponse,
)
async def crawler_job_review_content(
    job_id: str,
    item_id: str,
) -> CrawlReviewContentResponse:
    try:
        result = await get_review_content(job_id, item_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return CrawlReviewContentResponse(**result)


@router.post("/jobs/{job_id}/review", response_model=CrawlReviewResponse)
async def review_crawler_job(
    job_id: str,
    body: CrawlReviewActionRequest,
) -> CrawlReviewResponse:
    try:
        result = await apply_review(
            job_id,
            action=body.action,
            item_ids=body.item_ids,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return CrawlReviewResponse(**result)


@router.post("/jobs/{job_id}/pause", response_model=CrawlJobResponse)
async def pause_crawl_job(job_id: str) -> CrawlJobResponse:
    try:
        row = await CrawlerStore().request_pause(job_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _response(row)


@router.post("/jobs/{job_id}/resume", response_model=CrawlJobResponse)
async def resume_crawl_job(job_id: str) -> CrawlJobResponse:
    store = CrawlerStore()
    try:
        row, event = await store.resume(job_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await dispatch_eager(event)
    return _response(await store.get(row.id) or row)


@router.post("/jobs/{job_id}/stop", response_model=CrawlJobResponse)
async def stop_crawl_job(job_id: str) -> CrawlJobResponse:
    try:
        row = await CrawlerStore().request_cancel(job_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _response(row)
