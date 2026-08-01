"""Crawler 任务执行与 RAG 入库扇出。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from math import ceil
from typing import Any
from uuid import uuid4

import httpx

from app.core.crawler.cleaning import CrawlerCleaner
from app.core.crawler.engine import CrawlEngine, CrawlPage, CrawlRequest
from app.core.crawler.structured import (
    StructuredSourceRegistry,
    default_structured_registry,
)
from app.core.embedding.profiles import get_embedding_profile
from app.domain.crawler import CrawlJobStatus
from app.settings import get_settings
from app.stores.blob_store import get_blob_store
from app.stores.crawler_store import CrawlerStore
from app.stores.job_store import JobStore
from app.stores.knowledge_base_store import KnowledgeBaseStore

_FILENAME_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filename(page: CrawlPage) -> str:
    preferred = str(page.metadata.get("artifact_filename") or "").strip()
    if preferred:
        value = _FILENAME_UNSAFE.sub("_", preferred).strip(" ._")
        if value:
            stem = value[:-3] if value.lower().endswith(".md") else value
            return f"{stem[:157]}.md"
    value = _FILENAME_UNSAFE.sub("_", page.title).strip(" ._")
    return f"{(value or 'crawled-page')[:120]}.md"


class CrawlerRunner:
    def __init__(
        self,
        *,
        store: CrawlerStore | None = None,
        engine: CrawlEngine | None = None,
        cleaner: CrawlerCleaner | None = None,
        structured_registry: StructuredSourceRegistry | None = None,
        job_store: JobStore | None = None,
    ) -> None:
        self._store = store or CrawlerStore()
        self._engine = engine or CrawlEngine()
        self._cleaner = cleaner or CrawlerCleaner()
        self._structured = structured_registry or default_structured_registry()
        self._jobs = job_store or JobStore()

    async def run(self, crawl_job_id: str) -> None:
        job = await self._store.get(crawl_job_id)
        if job is None:
            return
        settings = get_settings()
        config = dict(job.config_json or {})
        require_review = bool(config.get("require_review"))
        review_mode = str(config.get("review_mode") or "human")
        review_criteria = str(config.get("review_criteria") or "").strip()
        request = CrawlRequest(
            urls=list(config.get("urls") or []),
            keywords=list(config.get("keywords") or []),
            site_urls=list(config.get("site_urls") or []),
            structured_sources=list(config.get("structured_sources") or []),
            source_options=dict(config.get("source_options") or {}),
            max_results_per_keyword=int(
                config.get("max_results_per_keyword") or settings.crawler_max_results_per_keyword
            ),
            max_pages_per_site=int(
                config.get("max_pages_per_site") or settings.crawler_max_pages_per_site
            ),
            max_total_pages=int(config.get("max_total_pages") or settings.crawler_max_total_pages),
            max_chars=int(config.get("max_chars") or settings.crawler_max_chars),
            timeout_seconds=float(
                config.get("timeout_seconds") or settings.crawler_timeout_seconds
            ),
            fetch_delay_seconds=float(
                config.get("fetch_delay_seconds")
                if config.get("fetch_delay_seconds") is not None
                else settings.crawler_fetch_delay_seconds
            ),
            max_retries=int(
                config.get("max_retries")
                if config.get("max_retries") is not None
                else settings.crawler_max_retries
            ),
            retry_base_seconds=float(
                config.get("retry_base_seconds")
                if config.get("retry_base_seconds") is not None
                else settings.crawler_retry_base_seconds
            ),
            force=bool(config.get("force")),
            allow_private_urls=settings.crawler_allow_private_urls,
            user_agent=settings.crawler_user_agent,
        )
        progress: dict[str, Any] = {
            **(job.progress_json or {}),
            "current_url": None,
            "errors": list((job.progress_json or {}).get("errors") or []),
            "rejections": list((job.progress_json or {}).get("rejections") or []),
            "review_mode": review_mode,
            "review_criteria": review_criteria,
        }
        min_content_chars = int(
            config.get("min_content_chars")
            if config.get("min_content_chars") is not None
            else settings.crawler_min_content_chars
        )

        async def control() -> str | None:
            return await self._store.control_state(crawl_job_id)

        category_routes: dict[str, str] = dict(
            progress.get("category_routes") or {}
        )

        async def category_knowledge_base_id(category: str) -> str:
            category = category.strip()
            if not category:
                return job.knowledge_base_id
            if category in category_routes:
                return category_routes[category]
            knowledge_bases = KnowledgeBaseStore()
            existing = await knowledge_bases.get_by_name(category)
            if existing is None:
                parent = await knowledge_bases.resolve(job.knowledge_base_id)
                profile = get_embedding_profile(parent.embedding_profile)
                try:
                    existing = await knowledge_bases.create(
                        name=category[:128],
                        description=(
                            "TrustGuard Agent 分类知识库；"
                            f"domain_category={config.get('domain_category') or 'legacy'}；"
                            "由数据采集任务自动创建。"
                        ),
                        profile=profile,
                    )
                except ValueError:
                    existing = await knowledge_bases.get_by_name(category)
                    if existing is None:
                        raise
            category_routes[category] = existing.id
            progress["category_routes"] = dict(category_routes)
            await self._store.update_progress(crawl_job_id, progress=progress)
            return existing.id

        async def target_knowledge_base_id(page: CrawlPage) -> str:
            if not bool(config.get("route_by_category")):
                return job.knowledge_base_id
            category = str(
                page.metadata.get("legacy_category")
                or config.get("target_category")
                or ""
            )
            return await category_knowledge_base_id(category)

        async def should_skip(
            url: str, knowledge_base_id: str | None = None
        ) -> bool:
            skipped = await self._store.is_url_crawled(
                knowledge_base_id or job.knowledge_base_id,
                url,
            )
            if skipped:
                progress["skipped"] = int(progress.get("skipped", 0)) + 1
                await self._store.update_progress(crawl_job_id, progress=progress)
            return skipped

        async def on_error(url: str, error: Exception) -> None:
            progress["failed"] = int(progress.get("failed", 0)) + 1
            progress["current_url"] = url
            errors = list(progress.get("errors") or [])
            errors.append({"url": url, "error": str(error)[:500]})
            progress["errors"] = errors[-100:]
            await self._store.record_url(
                knowledge_base_id=job.knowledge_base_id,
                url=url,
                status="failed",
                error=str(error),
            )
            await self._store.update_progress(crawl_job_id, progress=progress)

        async def queue_page(
            page: CrawlPage,
            *,
            knowledge_base_id: str | None = None,
        ) -> None:
            knowledge_base_id = knowledge_base_id or job.knowledge_base_id
            classification_metadata = {
                "domain_category": config.get("domain_category"),
                "kb_tier": config.get("kb_tier"),
                "agent_phases": list(config.get("agent_phases") or []),
                "topic_tags": list(config.get("topic_tags") or []),
                "category_priority": config.get("category_priority"),
                "category_name": config.get("target_category"),
                "crawler_preset_ids": list(config.get("preset_ids") or []),
            }
            for key, value in classification_metadata.items():
                if value not in (None, "", []):
                    page.metadata.setdefault(key, value)
            progress["discovered"] = int(progress.get("discovered", 0)) + 1
            progress["fetched"] = int(progress.get("fetched", 0)) + 1
            progress["current_url"] = page.url
            outcome = self._cleaner.clean(
                page,
                min_content_chars=min_content_chars,
            )
            if outcome.rejected:
                progress["rejected"] = int(progress.get("rejected", 0)) + 1
                rejections = list(progress.get("rejections") or [])
                rejections.append(
                    {
                        "url": page.url,
                        "reason": str(outcome.rejected_reason or "Rejected by cleaner")[:500],
                    }
                )
                progress["rejections"] = rejections[-100:]
                await self._store.record_url(
                    knowledge_base_id=knowledge_base_id,
                    url=page.url,
                    status="rejected",
                    content_hash=page.content_hash,
                    error=outcome.rejected_reason,
                )
                await self._store.update_progress(crawl_job_id, progress=progress)
                return
            page = outcome.page
            if page is None:
                raise RuntimeError("Crawler cleaner returned no page without a rejection")
            progress["cleaned"] = int(progress.get("cleaned", 0)) + 1
            if require_review:
                review_item = await self._stage_review(
                    knowledge_base_id=knowledge_base_id,
                    crawl_job_id=crawl_job_id,
                    page=page,
                )
                review_items = list(progress.get("review_items") or [])
                review_items.append(review_item)
                progress["review_items"] = review_items
                progress["pending_review"] = int(progress.get("pending_review", 0)) + 1
                progress["review_status"] = "pending"
                record_status = "pending_review"
                record_job_id = review_item["id"]
            else:
                ingest_job_id = await self._queue_ingest(
                    knowledge_base_id=knowledge_base_id,
                    crawl_job_id=crawl_job_id,
                    page=page,
                )
                progress["queued_for_ingest"] = int(progress.get("queued_for_ingest", 0)) + 1
                record_status = "queued_for_ingest"
                record_job_id = ingest_job_id
            await self._store.record_url(
                knowledge_base_id=knowledge_base_id,
                url=page.url,
                status=record_status,
                content_hash=page.content_hash,
                ingest_job_id=record_job_id,
            )
            await self._store.update_progress(crawl_job_id, progress=progress)
            if not require_review:
                await self._store.update_progress(
                    crawl_job_id,
                    progress=progress,
                    ingest_job_id=record_job_id,
                )

        try:
            has_web_sources = bool(
                request.urls or request.keywords or request.site_urls
            )
            fair_structured_limit = request.max_total_pages
            if has_web_sources and request.structured_sources:
                fair_structured_limit = ceil(
                    request.max_total_pages
                    / (len(request.structured_sources) + 1)
                )
            for source_id in request.structured_sources:
                if int(progress.get("fetched", 0)) >= request.max_total_pages:
                    break
                if await control() in {"pause", "cancel"}:
                    break
                adapter = self._structured.get(source_id)
                options = request.source_options.get(source_id) or {}
                requested_source_limit = max(
                    int(options.get("limit", adapter.info.default_limit)),
                    1,
                )
                if has_web_sources and "limit" not in options:
                    requested_source_limit = min(
                        requested_source_limit,
                        fair_structured_limit,
                    )
                source_limit = min(
                    requested_source_limit,
                    request.max_total_pages,
                    200,
                )
                source_limits = dict(progress.get("source_limits") or {})
                source_limits[source_id] = source_limit
                progress["source_limits"] = source_limits
                progress["current_source"] = source_id
                try:
                    async for page in adapter.crawl(
                        options,
                        limit=source_limit,
                        max_retries=request.max_retries,
                        retry_base_seconds=request.retry_base_seconds,
                        on_error=on_error,
                    ):
                        if await control() in {"pause", "cancel"}:
                            break
                        target_id = await target_knowledge_base_id(page)
                        if not request.force and await should_skip(
                            page.url, target_id
                        ):
                            continue
                        await queue_page(page, knowledge_base_id=target_id)
                        if int(progress.get("fetched", 0)) >= request.max_total_pages:
                            break
                except (httpx.HTTPError, ValueError, TypeError) as error:
                    await on_error(f"structured:{source_id}", error)

            remaining = request.max_total_pages - int(progress.get("fetched", 0))
            request.max_total_pages = max(remaining, 0)
            if remaining > 0:
                web_knowledge_base_id = job.knowledge_base_id
                if bool(config.get("route_by_category")):
                    web_knowledge_base_id = await category_knowledge_base_id(
                        str(config.get("target_category") or "")
                    )

                async def should_skip_web(url: str) -> bool:
                    return await should_skip(url, web_knowledge_base_id)

                async for page in self._engine.crawl(
                    request,
                    should_skip=should_skip_web,
                    control=control,
                    on_error=on_error,
                ):
                    await queue_page(
                        page,
                        knowledge_base_id=web_knowledge_base_id,
                    )
        except Exception as error:
            await self._store.finish(
                crawl_job_id,
                CrawlJobStatus.FAILED,
                progress=progress,
                error_message=str(error),
            )
            raise

        state = await control()
        progress["current_url"] = None
        if state not in {"cancel", "pause"} and require_review and review_mode == "agent":
            from app.core.crawler.agent_review import run_agent_review

            await self._store.update_progress(crawl_job_id, progress=progress)
            await run_agent_review(crawl_job_id, criteria=review_criteria)
            refreshed = await self._store.get(crawl_job_id)
            if refreshed is not None:
                progress = dict(refreshed.progress_json or progress)
        if state == "cancel":
            await self._store.finish(crawl_job_id, CrawlJobStatus.CANCELLED, progress=progress)
        elif state == "pause":
            await self._store.finish(crawl_job_id, CrawlJobStatus.PAUSED, progress=progress)
        elif (
            int(progress.get("queued_for_ingest", 0)) == 0
            and int(progress.get("pending_review", 0)) == 0
            and int(progress.get("failed", 0)) > 0
        ):
            await self._store.finish(
                crawl_job_id,
                CrawlJobStatus.FAILED,
                progress=progress,
                error_message="All discovered pages failed to fetch or extract",
            )
        else:
            await self._store.finish(crawl_job_id, CrawlJobStatus.SUCCEEDED, progress=progress)

    async def _stage_review(
        self,
        *,
        knowledge_base_id: str,
        crawl_job_id: str,
        page: CrawlPage,
    ) -> dict[str, Any]:
        """Persist cleaned crawler output without creating an ingest command."""
        knowledge_base = await KnowledgeBaseStore().resolve(knowledge_base_id)
        profile = get_embedding_profile(knowledge_base.embedding_profile)
        review_item_id = str(uuid4())
        raw = page.markdown.encode("utf-8")
        get_blob_store().put_job_upload(review_item_id, raw)
        return {
            "id": review_item_id,
            "status": "pending",
            "knowledge_base_id": knowledge_base_id,
            "title": page.title,
            "source_uri": page.url,
            "source_type": page.source_type,
            "original_filename": _filename(page),
            "content_preview": page.markdown[:800],
            "content_chars": len(page.markdown),
            "content_hash": page.content_hash,
            "created_at": _utc_iso(),
            "options": {
                "original_filename": _filename(page),
                "mime": "text/markdown",
                "source_uri": page.url,
                "source_metadata": {
                    **page.metadata,
                    "title": page.title,
                    "crawl_job_id": crawl_job_id,
                    "crawled_at": _utc_iso(),
                },
                "knowledge_base_id": knowledge_base_id,
                "embedding_profile": profile.id,
                "embedding_provider": profile.provider,
                "embedding_api_driver": profile.api_driver,
                "embedding_model": profile.model,
                "embedding_dim": profile.dimension,
                "embedding_query_instruction": profile.query_instruction,
            },
        }

    async def _queue_ingest(
        self,
        *,
        knowledge_base_id: str,
        crawl_job_id: str,
        page: CrawlPage,
    ) -> str:
        knowledge_base = await KnowledgeBaseStore().resolve(knowledge_base_id)
        profile = get_embedding_profile(knowledge_base.embedding_profile)
        ingest_job_id = str(uuid4())
        blob_store = get_blob_store()
        raw = page.markdown.encode("utf-8")
        blob_store.put_job_upload(ingest_job_id, raw)
        try:
            _, event = await self._jobs.create_ingest_command(
                job_id=ingest_job_id,
                source_type=page.source_type,
                source=page.url,
                knowledge_base_id=knowledge_base_id,
                options={
                    "original_filename": _filename(page),
                    "mime": "text/markdown",
                    "source_uri": page.url,
                    "source_metadata": {
                        **page.metadata,
                        "title": page.title,
                        "crawl_job_id": crawl_job_id,
                        "crawled_at": _utc_iso(),
                    },
                    "knowledge_base_id": knowledge_base_id,
                    "embedding_profile": profile.id,
                    "embedding_provider": profile.provider,
                    "embedding_api_driver": profile.api_driver,
                    "embedding_model": profile.model,
                    "embedding_dim": profile.dimension,
                    "embedding_query_instruction": profile.query_instruction,
                },
            )
        except Exception:
            blob_store.delete_job_staging(ingest_job_id)
            raise
        from app.workers.eager import dispatch_eager

        await dispatch_eager(event)
        return ingest_job_id


def get_crawler_runner() -> CrawlerRunner:
    return CrawlerRunner()
