"""Crawler 任务、控制状态与 URL 去重存储。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.crawler import CRAWL_TERMINAL_STATUSES, CrawlJobStatus
from app.settings import get_settings
from app.stores.db import get_engine
from app.stores.models import CrawlJobRow, CrawlUrlRecordRow, KnowledgeBaseRow
from app.stores.outbox_store import OutboxEvent, add_outbox_event, event_from_row
from app.workers.messages import RUN_CRAWLER


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class CrawlerStore:
    async def create_job(
        self,
        *,
        knowledge_base_id: str,
        config: dict[str, Any],
        job_id: str | None = None,
    ) -> tuple[CrawlJobRow, OutboxEvent]:
        """原子创建采集任务和 Outbox 命令。"""
        crawl_job_id = job_id or str(uuid4())
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            knowledge_base = await session.get(KnowledgeBaseRow, knowledge_base_id)
            if knowledge_base is None:
                raise LookupError("Knowledge base not found")
            row = CrawlJobRow(
                id=crawl_job_id,
                knowledge_base_id=knowledge_base_id,
                status=CrawlJobStatus.QUEUED,
                config_json=config,
                progress_json={
                    "discovered": 0,
                    "fetched": 0,
                    "queued_for_ingest": 0,
                    "skipped": 0,
                    "failed": 0,
                    "errors": [],
                },
                ingest_job_ids_json=[],
            )
            session.add(row)
            event_row = add_outbox_event(
                session,
                event_type=RUN_CRAWLER,
                aggregate_id=crawl_job_id,
                payload={"crawl_job_id": crawl_job_id},
            )
            await session.commit()
            return row, event_from_row(event_row)

    async def get(self, job_id: str) -> CrawlJobRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(CrawlJobRow, job_id)

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        knowledge_base_id: str | None = None,
    ) -> tuple[list[CrawlJobRow], int]:
        filters = []
        if knowledge_base_id:
            filters.append(CrawlJobRow.knowledge_base_id == knowledge_base_id)
        async with AsyncSession(get_engine()) as session:
            count_query = select(func.count()).select_from(CrawlJobRow)
            rows_query = select(CrawlJobRow)
            if filters:
                count_query = count_query.where(*filters)
                rows_query = rows_query.where(*filters)
            total = int((await session.execute(count_query)).scalar_one())
            result = await session.execute(
                rows_query.order_by(CrawlJobRow.created_at.desc(), CrawlJobRow.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return list(result.scalars().all()), total

    async def claim(self, job_id: str) -> bool:
        """认领排队或可重试任务；终态和仍在运行的任务不重复执行。"""
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None or row.status in CRAWL_TERMINAL_STATUSES:
                return False
            if row.status not in {CrawlJobStatus.QUEUED, CrawlJobStatus.FAILED}:
                return False
            if row.attempt >= row.max_attempts:
                return False
            now = _utcnow()
            row.status = CrawlJobStatus.RUNNING
            row.attempt += 1
            row.started_at = row.started_at or now
            row.finished_at = None
            row.error_message = None
            row.cancel_requested = False
            row.pause_requested = False
            row.updated_at = now
            await session.commit()
            return True

    async def control_state(self, job_id: str) -> str | None:
        row = await self.get(job_id)
        if row is None:
            return "cancel"
        if row.cancel_requested or row.status == CrawlJobStatus.CANCELLED:
            return "cancel"
        if row.pause_requested or row.status == CrawlJobStatus.PAUSED:
            return "pause"
        return None

    async def update_progress(
        self,
        job_id: str,
        *,
        progress: dict[str, Any] | None = None,
        ingest_job_id: str | None = None,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                return
            if progress is not None:
                row.progress_json = progress
            if ingest_job_id:
                ids = list(row.ingest_job_ids_json or [])
                if ingest_job_id not in ids:
                    ids.append(ingest_job_id)
                row.ingest_job_ids_json = ids
            row.updated_at = _utcnow()
            await session.commit()

    async def finish(
        self,
        job_id: str,
        status: CrawlJobStatus,
        *,
        progress: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                return
            row.status = status
            if progress is not None:
                row.progress_json = progress
            row.error_message = error_message[:4000] if error_message else None
            row.finished_at = None if status == CrawlJobStatus.PAUSED else _utcnow()
            row.updated_at = _utcnow()
            await session.commit()

    async def request_pause(self, job_id: str) -> CrawlJobRow:
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler job not found")
            if row.status != CrawlJobStatus.RUNNING:
                raise ValueError("Only running crawler jobs can be paused")
            row.pause_requested = True
            row.updated_at = _utcnow()
            await session.commit()
            return row

    async def request_cancel(self, job_id: str) -> CrawlJobRow:
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler job not found")
            if row.status in CRAWL_TERMINAL_STATUSES:
                return row
            row.cancel_requested = True
            if row.status in {
                CrawlJobStatus.QUEUED,
                CrawlJobStatus.PAUSED,
                CrawlJobStatus.FAILED,
            }:
                row.status = CrawlJobStatus.CANCELLED
                row.finished_at = _utcnow()
            row.updated_at = _utcnow()
            await session.commit()
            return row

    async def resume(self, job_id: str) -> tuple[CrawlJobRow, OutboxEvent]:
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler job not found")
            if row.status not in {CrawlJobStatus.PAUSED, CrawlJobStatus.FAILED}:
                raise ValueError("Only paused or failed crawler jobs can be resumed")
            if row.attempt >= row.max_attempts:
                row.attempt = 0
            row.status = CrawlJobStatus.QUEUED
            row.pause_requested = False
            row.cancel_requested = False
            row.finished_at = None
            row.updated_at = _utcnow()
            event_row = add_outbox_event(
                session,
                event_type=RUN_CRAWLER,
                aggregate_id=job_id,
                payload={"crawl_job_id": job_id},
            )
            await session.commit()
            return row, event_from_row(event_row)

    async def is_url_crawled(self, knowledge_base_id: str, url: str) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(
                CrawlUrlRecordRow,
                {"knowledge_base_id": knowledge_base_id, "url_hash": _url_hash(url)},
            )
            return bool(
                row
                and row.last_status
                in {
                    "pending_review",
                    "queued_for_ingest",
                    "rejected",
                    "rejected_by_review",
                }
            )

    async def record_url(
        self,
        *,
        knowledge_base_id: str,
        url: str,
        status: str,
        content_hash: str | None = None,
        ingest_job_id: str | None = None,
        error: str | None = None,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            key = {"knowledge_base_id": knowledge_base_id, "url_hash": _url_hash(url)}
            row = await session.get(CrawlUrlRecordRow, key, with_for_update=True)
            if row is None:
                row = CrawlUrlRecordRow(**key, url=url)
                session.add(row)
            row.url = url
            row.content_hash = content_hash
            row.ingest_job_id = ingest_job_id
            row.last_status = status
            row.last_error = error[:4000] if error else None
            row.last_crawled_at = _utcnow()
            row.updated_at = _utcnow()
            await session.commit()

    async def recover_stale_jobs(self) -> list[OutboxEvent]:
        """重新排队长时间没有进度更新的运行中任务。"""
        stale_before = _utcnow() - timedelta(seconds=get_settings().crawler_stale_seconds)
        events: list[OutboxEvent] = []
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            result = await session.execute(
                select(CrawlJobRow)
                .where(
                    CrawlJobRow.status == CrawlJobStatus.RUNNING,
                    CrawlJobRow.updated_at <= stale_before,
                )
                .with_for_update(skip_locked=True)
            )
            for row in result.scalars():
                row.status = CrawlJobStatus.FAILED
                row.error_message = "Crawler worker heartbeat expired; task was recovered"
                row.updated_at = _utcnow()
                event_row = add_outbox_event(
                    session,
                    event_type=RUN_CRAWLER,
                    aggregate_id=row.id,
                    payload={"crawl_job_id": row.id},
                )
                events.append(event_from_row(event_row))
            await session.commit()
        return events


def get_crawler_store() -> CrawlerStore:
    return CrawlerStore()
