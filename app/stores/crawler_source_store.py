"""Persistent crawler source registry, schedules, validators, and version history."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import IngestJobStatus
from app.domain.crawler import CrawlJobStatus
from app.stores.db import get_engine
from app.stores.models import (
    CrawlerResourceStateRow,
    CrawlJobRow,
    CrawlerSourceRow,
    CrawlerSourceRunRow,
    CrawlerSourceVersionRow,
    IngestJobRow,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _next_run(interval_minutes: int | None, *, now: datetime | None = None) -> datetime | None:
    if not interval_minutes:
        return None
    return (now or _utcnow()) + timedelta(minutes=interval_minutes)


async def ensure_crawler_source_schema() -> None:
    """Create the four additive crawler source tables for existing deployments."""
    from app.stores.models import Base

    tables = (
        Base.metadata.tables["crawler_sources"],
        Base.metadata.tables["crawler_source_runs"],
        Base.metadata.tables["crawler_resource_states"],
        Base.metadata.tables["crawler_source_versions"],
    )
    async with get_engine().begin() as connection:
        for table in tables:
            await connection.run_sync(lambda sync_connection, item=table: item.create(
                sync_connection, checkfirst=True
            ))


class CrawlerSourceStore:
    async def create(self, values: dict[str, Any]) -> CrawlerSourceRow:
        now = _utcnow()
        row = CrawlerSourceRow(
            id=str(values.get("id") or uuid4()),
            knowledge_base_id=str(values["knowledge_base_id"]),
            name=str(values["name"]),
            description=values.get("description"),
            source_kind=str(values["source_kind"]),
            endpoint=values.get("endpoint"),
            preset_ids_json=list(values.get("preset_ids") or []),
            config_json=dict(values.get("config") or {}),
            trust_level=str(values.get("trust_level") or "trusted"),
            content_type=str(values.get("content_type") or "security_knowledge"),
            usage_restrictions=values.get("usage_restrictions"),
            enabled=bool(values.get("enabled", True)),
            schedule_enabled=bool(values.get("schedule_enabled", False)),
            schedule_interval_minutes=values.get("schedule_interval_minutes"),
            next_run_at=(
                values.get("next_run_at")
                or _next_run(values.get("schedule_interval_minutes"), now=now)
                if values.get("schedule_enabled")
                else None
            ),
        )
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            session.add(row)
            await session.commit()
            return row

    async def get(self, source_id: str) -> CrawlerSourceRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(CrawlerSourceRow, source_id)

    async def list(
        self,
        *,
        enabled: bool | None = None,
        knowledge_base_id: str | None = None,
    ) -> list[CrawlerSourceRow]:
        query = select(CrawlerSourceRow)
        if enabled is not None:
            query = query.where(CrawlerSourceRow.enabled == enabled)
        if knowledge_base_id:
            query = query.where(CrawlerSourceRow.knowledge_base_id == knowledge_base_id)
        query = query.order_by(CrawlerSourceRow.name, CrawlerSourceRow.id)
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(query)
            return list(result.scalars().all())

    async def update(self, source_id: str, values: dict[str, Any]) -> CrawlerSourceRow:
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            row = await session.get(CrawlerSourceRow, source_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler source not found")
            mapping = {
                "knowledge_base_id": "knowledge_base_id",
                "name": "name",
                "description": "description",
                "source_kind": "source_kind",
                "endpoint": "endpoint",
                "preset_ids": "preset_ids_json",
                "config": "config_json",
                "trust_level": "trust_level",
                "content_type": "content_type",
                "usage_restrictions": "usage_restrictions",
                "enabled": "enabled",
                "schedule_enabled": "schedule_enabled",
                "schedule_interval_minutes": "schedule_interval_minutes",
                "next_run_at": "next_run_at",
            }
            for key, attribute in mapping.items():
                if key in values:
                    value = values[key]
                    if key == "preset_ids":
                        value = list(value or [])
                    elif key == "config":
                        value = dict(value or {})
                    setattr(row, attribute, value)
            if "next_run_at" not in values and {
                "schedule_enabled",
                "schedule_interval_minutes",
            } & values.keys():
                row.next_run_at = (
                    _next_run(row.schedule_interval_minutes)
                    if row.enabled and row.schedule_enabled
                    else None
                )
            row.updated_at = _utcnow()
            await session.commit()
            return row

    async def delete(self, source_id: str) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlerSourceRow, source_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def configs_for_presets(self, preset_ids: list[str]) -> list[CrawlerSourceRow]:
        selected = set(preset_ids)
        if not selected:
            return []
        rows = await self.list(enabled=True)
        return [row for row in rows if selected.intersection(row.preset_ids_json or [])]

    async def claim_due(self, *, limit: int = 20) -> list[CrawlerSourceRow]:
        now = _utcnow()
        active_job_exists = exists(
            select(CrawlerSourceRunRow.crawl_job_id)
            .join(
                CrawlJobRow,
                CrawlJobRow.id == CrawlerSourceRunRow.crawl_job_id,
            )
            .where(
                CrawlerSourceRunRow.source_id == CrawlerSourceRow.id,
                CrawlJobRow.status.in_(
                    {
                        CrawlJobStatus.QUEUED,
                        CrawlJobStatus.RUNNING,
                        CrawlJobStatus.PAUSED,
                    }
                ),
            )
        )
        pending_review_exists = exists(
            select(CrawlerSourceRunRow.crawl_job_id)
            .join(
                CrawlJobRow,
                CrawlJobRow.id == CrawlerSourceRunRow.crawl_job_id,
            )
            .where(
                CrawlerSourceRunRow.source_id == CrawlerSourceRow.id,
                CrawlJobRow.progress_json["pending_review"].as_integer() > 0,
            )
        )
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            result = await session.execute(
                select(CrawlerSourceRow)
                .where(
                    CrawlerSourceRow.enabled.is_(True),
                    CrawlerSourceRow.schedule_enabled.is_(True),
                    CrawlerSourceRow.schedule_interval_minutes.is_not(None),
                    CrawlerSourceRow.next_run_at.is_not(None),
                    CrawlerSourceRow.next_run_at <= now,
                    ~active_job_exists,
                    ~pending_review_exists,
                )
                .order_by(CrawlerSourceRow.next_run_at, CrawlerSourceRow.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = list(result.scalars().all())
            for row in rows:
                row.last_run_at = now
                row.next_run_at = _next_run(row.schedule_interval_minutes, now=now)
                row.updated_at = now
            await session.commit()
            return rows

    async def register_run(
        self,
        *,
        source_id: str,
        crawl_job_id: str,
        trigger_type: str,
    ) -> None:
        now = _utcnow()
        async with AsyncSession(get_engine()) as session:
            source = await session.get(CrawlerSourceRow, source_id, with_for_update=True)
            if source is None:
                raise LookupError("Crawler source not found")
            source.last_job_id = crawl_job_id
            source.last_run_at = now
            source.updated_at = now
            session.add(
                CrawlerSourceRunRow(
                    crawl_job_id=crawl_job_id,
                    source_id=source_id,
                    trigger_type=trigger_type,
                    status=CrawlJobStatus.QUEUED.value,
                    progress_json={},
                )
            )
            await session.commit()

    async def finish_run(
        self,
        *,
        crawl_job_id: str,
        status: CrawlJobStatus,
        progress: dict[str, Any],
        error_message: str | None = None,
    ) -> None:
        now = _utcnow()
        async with AsyncSession(get_engine()) as session:
            run = await session.get(CrawlerSourceRunRow, crawl_job_id, with_for_update=True)
            if run is None:
                return
            run.status = status.value
            run.progress_json = dict(progress)
            run.error_message = error_message
            if status in {
                CrawlJobStatus.SUCCEEDED,
                CrawlJobStatus.FAILED,
                CrawlJobStatus.CANCELLED,
            }:
                run.finished_at = now
            run.updated_at = now
            source = await session.get(CrawlerSourceRow, run.source_id, with_for_update=True)
            if source is not None and status == CrawlJobStatus.SUCCEEDED:
                source.last_success_at = now
                source.updated_at = now
            await session.commit()

    async def note_review(
        self,
        *,
        crawl_job_id: str,
        action: str,
        version_id: str | None = None,
        ingest_job_id: str | None = None,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            run = await session.get(CrawlerSourceRunRow, crawl_job_id, with_for_update=True)
            if run is not None:
                if action == "approve":
                    run.approved_count += 1
                elif action == "reject":
                    run.rejected_count += 1
                run.updated_at = _utcnow()
            if version_id:
                version = await session.get(
                    CrawlerSourceVersionRow,
                    version_id,
                    with_for_update=True,
                )
                if version is not None:
                    version.status = "approved" if action == "approve" else "rejected"
                    if ingest_job_id:
                        version.ingest_job_id = ingest_job_id
                    version.updated_at = _utcnow()
            await session.commit()

    async def conditional_headers(self, source_id: str, url: str) -> dict[str, str]:
        key = {"source_id": source_id, "url_hash": _url_hash(url)}
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlerResourceStateRow, key)
        headers: dict[str, str] = {}
        if row and row.etag:
            headers["If-None-Match"] = row.etag
        if row and row.last_modified:
            headers["If-Modified-Since"] = row.last_modified
        return headers

    async def record_http_state(
        self,
        *,
        source_id: str,
        url: str,
        etag: str | None,
        last_modified: str | None,
        not_modified: bool,
    ) -> None:
        now = _utcnow()
        key = {"source_id": source_id, "url_hash": _url_hash(url)}
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlerResourceStateRow, key, with_for_update=True)
            if row is None:
                row = CrawlerResourceStateRow(**key, url=url)
                session.add(row)
            row.url = url
            if etag:
                row.etag = etag[:512]
            if last_modified:
                row.last_modified = last_modified[:512]
            row.last_seen_at = now
            if not_modified and row.status != "superseded":
                row.status = "active"
            row.updated_at = now
            await session.commit()

    async def is_duplicate(self, source_id: str, url: str, content_hash: str) -> bool:
        url_hash = _url_hash(url)
        async with AsyncSession(get_engine()) as session:
            resource = await session.get(
                CrawlerResourceStateRow,
                {"source_id": source_id, "url_hash": url_hash},
            )
            if resource and resource.current_content_hash == content_hash:
                return True
            existing = await session.execute(
                select(CrawlerSourceVersionRow.id).where(
                    CrawlerSourceVersionRow.source_id == source_id,
                    CrawlerSourceVersionRow.url_hash == url_hash,
                    CrawlerSourceVersionRow.content_hash == content_hash,
                    CrawlerSourceVersionRow.status.not_in(("failed", "rejected")),
                )
            )
            return existing.scalar_one_or_none() is not None

    async def record_version(
        self,
        *,
        source_id: str,
        resource_url: str,
        crawl_job_id: str,
        content_hash: str,
        ingest_job_id: str | None,
        status: str,
    ) -> CrawlerSourceVersionRow:
        now = _utcnow()
        url_hash = _url_hash(resource_url)
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            resource = await session.get(
                CrawlerResourceStateRow,
                {"source_id": source_id, "url_hash": url_hash},
                with_for_update=True,
            )
            if resource is None:
                resource = CrawlerResourceStateRow(
                    source_id=source_id,
                    url_hash=url_hash,
                    url=resource_url,
                    last_seen_at=now,
                )
                session.add(resource)
            latest = await session.execute(
                select(CrawlerSourceVersionRow)
                .where(
                    CrawlerSourceVersionRow.source_id == source_id,
                    CrawlerSourceVersionRow.url_hash == url_hash,
                )
                .order_by(CrawlerSourceVersionRow.version.desc())
                .limit(1)
            )
            previous = latest.scalar_one_or_none()
            row = CrawlerSourceVersionRow(
                id=str(uuid4()),
                source_id=source_id,
                url_hash=url_hash,
                resource_url=resource_url,
                crawl_job_id=crawl_job_id,
                ingest_job_id=ingest_job_id,
                content_hash=content_hash,
                version=(previous.version + 1 if previous else 1),
                status=status,
                supersedes_version_id=(previous.id if previous else None),
            )
            session.add(row)
            resource.url = resource_url
            resource.last_seen_at = now
            resource.updated_at = now
            await session.commit()
            return row

    async def reconcile_versions(self, *, limit: int = 200) -> int:
        terminal = {
            IngestJobStatus.SUCCEEDED,
            IngestJobStatus.DEDUPLICATED,
            IngestJobStatus.FAILED,
            IngestJobStatus.CANCELLED,
            IngestJobStatus.DISCARDED,
        }
        now = _utcnow()
        changed = 0
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(CrawlerSourceVersionRow, IngestJobRow)
                .join(
                    IngestJobRow,
                    IngestJobRow.id == CrawlerSourceVersionRow.ingest_job_id,
                )
                .where(
                    CrawlerSourceVersionRow.status.in_(("queued", "approved")),
                    IngestJobRow.status.in_(tuple(terminal)),
                )
                .order_by(CrawlerSourceVersionRow.updated_at)
                .limit(limit)
                .with_for_update()
            )
            for version, ingest_job in result.all():
                if ingest_job.status in {
                    IngestJobStatus.SUCCEEDED,
                    IngestJobStatus.DEDUPLICATED,
                }:
                    resource = await session.get(
                        CrawlerResourceStateRow,
                        {"source_id": version.source_id, "url_hash": version.url_hash},
                        with_for_update=True,
                    )
                    if resource is None:
                        continue
                    previous = None
                    if version.supersedes_version_id:
                        previous = await session.get(
                            CrawlerSourceVersionRow,
                            version.supersedes_version_id,
                            with_for_update=True,
                        )
                    if previous is not None and previous.status == "active":
                        previous.status = "superseded"
                        previous.superseded_at = now
                        previous.updated_at = now
                    version.status = "active"
                    version.document_id = ingest_job.document_id or ingest_job.pending_document_id
                    version.activated_at = now
                    resource.current_content_hash = version.content_hash
                    resource.current_ingest_job_id = ingest_job.id
                    resource.current_document_id = version.document_id
                    resource.current_version = version.version
                    resource.status = "active"
                    resource.last_changed_at = now
                    resource.last_seen_at = now
                    resource.updated_at = now
                else:
                    version.status = "failed"
                version.updated_at = now
                changed += 1
            await session.commit()
        return changed

    async def stats(self, source_id: str) -> dict[str, Any]:
        async with AsyncSession(get_engine()) as session:
            source = await session.get(CrawlerSourceRow, source_id)
            if source is None:
                raise LookupError("Crawler source not found")
            runs_result = await session.execute(
                select(CrawlerSourceRunRow)
                .where(CrawlerSourceRunRow.source_id == source_id)
                .order_by(CrawlerSourceRunRow.created_at.desc())
            )
            runs = list(runs_result.scalars().all())
            resource_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrawlerResourceStateRow)
                        .where(CrawlerResourceStateRow.source_id == source_id)
                    )
                ).scalar_one()
            )
            active_versions = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrawlerSourceVersionRow)
                        .where(
                            CrawlerSourceVersionRow.source_id == source_id,
                            CrawlerSourceVersionRow.status == "active",
                        )
                    )
                ).scalar_one()
            )
        progress = [dict(run.progress_json or {}) for run in runs]
        fetched = sum(int(item.get("fetched", 0)) for item in progress)
        duplicates = sum(int(item.get("duplicates", 0)) for item in progress)
        not_modified = sum(int(item.get("not_modified", 0)) for item in progress)
        failed_items = sum(int(item.get("failed", 0)) for item in progress)
        approved = sum(run.approved_count for run in runs)
        rejected = sum(run.rejected_count for run in runs)
        successful_runs = sum(run.status == CrawlJobStatus.SUCCEEDED.value for run in runs)
        failed_runs = sum(run.status == CrawlJobStatus.FAILED.value for run in runs)
        considered = fetched + not_modified
        reviewed = approved + rejected
        now = _utcnow()
        freshness_seconds = (
            int((now - source.last_success_at).total_seconds())
            if source.last_success_at
            else None
        )
        stale_after_seconds = (
            source.schedule_interval_minutes * 120
            if source.schedule_interval_minutes
            else None
        )
        return {
            "run_count": len(runs),
            "successful_runs": successful_runs,
            "failed_runs": failed_runs,
            "success_rate": round(successful_runs / len(runs), 4) if runs else None,
            "fetched": fetched,
            "duplicates": duplicates,
            "not_modified": not_modified,
            "failed_items": failed_items,
            "duplicate_rate": round((duplicates + not_modified) / considered, 4)
            if considered
            else None,
            "approved": approved,
            "rejected": rejected,
            "approval_rate": round(approved / reviewed, 4) if reviewed else None,
            "resource_count": resource_count,
            "active_versions": active_versions,
            "freshness_seconds": freshness_seconds,
            "freshness_status": (
                "never"
                if freshness_seconds is None
                else "stale"
                if stale_after_seconds and freshness_seconds > stale_after_seconds
                else "fresh"
            ),
        }

    async def list_versions(
        self,
        source_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[CrawlerSourceVersionRow], int]:
        async with AsyncSession(get_engine()) as session:
            total = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CrawlerSourceVersionRow)
                        .where(CrawlerSourceVersionRow.source_id == source_id)
                    )
                ).scalar_one()
            )
            result = await session.execute(
                select(CrawlerSourceVersionRow)
                .where(CrawlerSourceVersionRow.source_id == source_id)
                .order_by(
                    CrawlerSourceVersionRow.version.desc(),
                    CrawlerSourceVersionRow.id.desc(),
                )
                .offset(offset)
                .limit(limit)
            )
            return list(result.scalars().all()), total


async def seed_category_preset_sources() -> int:
    """Seed the nine category presets once; later database edits remain authoritative."""
    from app.core.crawler.presets import CRAWLER_CATEGORY_PRESETS, expand_crawler_presets
    from app.stores.knowledge_base_store import KnowledgeBaseStore

    knowledge_base = await KnowledgeBaseStore().get_default()
    store = CrawlerSourceStore()
    created = 0
    for preset in CRAWLER_CATEGORY_PRESETS:
        source_id = f"preset:{preset.id}"
        if await store.get(source_id) is not None:
            continue
        expanded = expand_crawler_presets([preset.id])
        await store.create(
            {
                "id": source_id,
                "knowledge_base_id": knowledge_base.id,
                "name": preset.name,
                "description": preset.description,
                "source_kind": "preset",
                "preset_ids": [preset.id],
                "trust_level": "trusted",
                "content_type": preset.domain_category or "security_knowledge",
                "usage_restrictions": "仅用于 TrustGuard 安全知识采集与检索增强。",
                "enabled": True,
                "schedule_enabled": False,
                "config": {
                    "preset_ids": [preset.id],
                    "site_urls": expanded.site_urls,
                    "keywords": expanded.keywords,
                    "structured_sources": expanded.structured_sources,
                    "source_options": expanded.source_options,
                    "target_category": expanded.category_name,
                    "domain_category": expanded.domain_category,
                    "kb_tier": expanded.kb_tier,
                    "agent_phases": expanded.phases,
                    "topic_tags": expanded.topic_tags,
                    "category_priority": expanded.priority,
                    "review_criteria": expanded.review_criteria,
                    "route_by_category": True,
                    "require_review": True,
                    "review_mode": "human",
                },
            }
        )
        created += 1
    return created


def get_crawler_source_store() -> CrawlerSourceStore:
    return CrawlerSourceStore()
