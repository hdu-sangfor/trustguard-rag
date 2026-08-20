"""Crawler 任务、控制状态与 URL 去重存储。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.crawler import CRAWL_TERMINAL_STATUSES, CrawlJobStatus
from app.settings import get_settings
from app.stores.db import get_engine
from app.stores.models import (
    CrawlJobRow,
    CrawlerSourceRow,
    CrawlerSourceRunRow,
    CrawlUrlRecordRow,
    KnowledgeBaseRow,
)
from app.stores.outbox_store import OutboxEvent, add_outbox_event, event_from_row
from app.workers.messages import RUN_CRAWLER


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _new_crawl_job(
    *,
    crawl_job_id: str,
    knowledge_base_id: str,
    config: dict[str, Any],
) -> CrawlJobRow:
    return CrawlJobRow(
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


def _refresh_review_counts(progress: dict[str, Any]) -> None:
    items = list(progress.get("review_items") or [])
    pending = sum(
        item.get("status") in {"pending", "processing", "rejecting"}
        for item in items
    )
    progress["pending_review"] = pending
    progress["review_status"] = "pending" if pending else "completed"


def _merge_runner_review_progress(
    current: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Preserve review decisions made while a runner appends new staged items."""
    current_items = {
        str(item.get("id") or ""): dict(item)
        for item in current.get("review_items") or []
        if item.get("id")
    }
    if not current_items:
        return incoming
    merged_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in incoming.get("review_items") or []:
        item = dict(raw_item)
        item_id = str(item.get("id") or "")
        if item_id in current_items:
            item.update(current_items[item_id])
        merged_items.append(item)
        seen.add(item_id)
    merged_items.extend(
        item for item_id, item in current_items.items() if item_id not in seen
    )
    incoming["review_items"] = merged_items
    incoming["queued_for_ingest"] = max(
        int(current.get("queued_for_ingest", 0)),
        int(incoming.get("queued_for_ingest", 0)),
    )
    _refresh_review_counts(incoming)
    return incoming


def _review_claim_expired(
    item: dict[str, Any],
    *,
    stale_before: datetime,
) -> bool:
    raw_value = str(item.get("review_claimed_at") or "").strip()
    if not raw_value:
        return True
    try:
        claimed_at = datetime.fromisoformat(raw_value)
    except ValueError:
        return True
    if claimed_at.tzinfo is not None:
        claimed_at = claimed_at.astimezone(timezone.utc).replace(tzinfo=None)
    return claimed_at <= stale_before


def _review_timestamp(value: Any) -> datetime | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@dataclass(frozen=True, slots=True)
class ReviewContentCleanupClaim:
    crawl_job_id: str
    item_id: str
    claim_token: str


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
            row = _new_crawl_job(
                crawl_job_id=crawl_job_id,
                knowledge_base_id=knowledge_base_id,
                config=config,
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

    async def create_source_job(
        self,
        *,
        source_id: str,
        config: dict[str, Any],
        trigger_type: str,
        job_id: str | None = None,
    ) -> tuple[CrawlJobRow, OutboxEvent]:
        """Atomically create one non-overlapping run for a managed source."""
        crawl_job_id = job_id or str(uuid4())
        now = _utcnow()
        active_statuses = (
            CrawlJobStatus.QUEUED,
            CrawlJobStatus.RUNNING,
            CrawlJobStatus.PAUSED,
        )
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            source = await session.get(
                CrawlerSourceRow,
                source_id,
                with_for_update=True,
            )
            if source is None:
                raise LookupError("Crawler source not found")
            if not source.enabled:
                raise ValueError("Crawler source is disabled")
            active_job_id = await session.scalar(
                select(CrawlJobRow.id)
                .join(
                    CrawlerSourceRunRow,
                    CrawlerSourceRunRow.crawl_job_id == CrawlJobRow.id,
                )
                .where(
                    CrawlerSourceRunRow.source_id == source_id,
                    CrawlJobRow.status.in_(active_statuses),
                )
                .limit(1)
            )
            if active_job_id is not None:
                raise ValueError(
                    f"Crawler source already has an active run: {active_job_id}"
                )
            knowledge_base = await session.get(
                KnowledgeBaseRow,
                source.knowledge_base_id,
            )
            if knowledge_base is None:
                raise LookupError("Knowledge base not found")
            row = _new_crawl_job(
                crawl_job_id=crawl_job_id,
                knowledge_base_id=source.knowledge_base_id,
                config=config,
            )
            session.add(row)
            session.add(
                CrawlerSourceRunRow(
                    crawl_job_id=crawl_job_id,
                    source_id=source_id,
                    trigger_type=trigger_type,
                    status=CrawlJobStatus.QUEUED.value,
                    progress_json={},
                )
            )
            source.last_job_id = crawl_job_id
            source.last_run_at = now
            source.updated_at = now
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

    async def heartbeat(self, job_id: str, expected_attempt: int) -> bool:
        """Renew a running crawler claim, fenced by its attempt generation."""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                update(CrawlJobRow)
                .where(
                    CrawlJobRow.id == job_id,
                    CrawlJobRow.status == CrawlJobStatus.RUNNING,
                    CrawlJobRow.attempt == expected_attempt,
                )
                .values(updated_at=_utcnow())
            )
            await session.commit()
            return bool(result.rowcount)

    async def control_state(
        self,
        job_id: str,
        *,
        expected_attempt: int | None = None,
    ) -> str | None:
        row = await self.get(job_id)
        if row is None:
            return "cancel"
        if expected_attempt is not None and (
            row.attempt != expected_attempt or row.status != CrawlJobStatus.RUNNING
        ):
            return "lost"
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
        expected_attempt: int | None = None,
    ) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                return False
            if expected_attempt is not None and (
                row.attempt != expected_attempt
                or row.status != CrawlJobStatus.RUNNING
            ):
                return False
            if progress is not None:
                incoming = dict(progress)
                if expected_attempt is not None:
                    incoming = _merge_runner_review_progress(
                        dict(row.progress_json or {}), incoming
                    )
                row.progress_json = incoming
            if ingest_job_id:
                ids = list(row.ingest_job_ids_json or [])
                if ingest_job_id not in ids:
                    ids.append(ingest_job_id)
                row.ingest_job_ids_json = ids
            row.updated_at = _utcnow()
            await session.commit()
            return True

    async def claim_review_items(
        self,
        job_id: str,
        *,
        action: str,
        item_ids: list[str],
        allow_rejected_approval: bool = False,
    ) -> list[dict[str, Any]]:
        """Atomically claim pending review items for one approve/reject action."""
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler job not found")
            progress = dict(row.progress_json or {})
            items = [dict(item) for item in progress.get("review_items") or []]
            index = {str(item.get("id") or ""): item for item in items}
            unknown = set(item_ids) - set(index)
            if unknown:
                raise LookupError("Review item not found")
            claimed: list[dict[str, Any]] = []
            claimed_status = "processing" if action == "approve" else "rejecting"
            now = _utcnow()
            stale_before = now - timedelta(
                seconds=get_settings().crawler_review_claim_seconds
            )
            for item_id in dict.fromkeys(item_ids):
                item = index[item_id]
                status = str(item.get("status") or "")
                expires_at = _review_timestamp(item.get("review_content_expires_at"))
                rejected_approval = (
                    action == "approve"
                    and allow_rejected_approval
                    and status == "rejected"
                    and item.get("reviewer") == "agent"
                    and item.get("agent_decision") == "reject"
                    and bool(item.get("review_content_available"))
                    and expires_at is not None
                    and expires_at > now
                )
                claimable = status == "pending" or rejected_approval or (
                    status in {"processing", "rejecting"}
                    and _review_claim_expired(item, stale_before=stale_before)
                )
                if not claimable:
                    continue
                previous_status = str(item.get("review_previous_status") or "")
                item["review_previous_status"] = (
                    previous_status
                    if previous_status in {"pending", "rejected"}
                    else status if status in {"pending", "rejected"} else "pending"
                )
                item["status"] = claimed_status
                item["review_claim_token"] = str(uuid4())
                item["review_claimed_at"] = now.isoformat()
                claimed.append(dict(item))
            progress["review_items"] = items
            _refresh_review_counts(progress)
            row.progress_json = progress
            row.updated_at = _utcnow()
            await session.commit()
            return claimed

    async def claim_expired_review_content(
        self,
        *,
        limit: int = 200,
    ) -> list[ReviewContentCleanupClaim]:
        """Fence expired Agent-rejected staging objects before deleting them."""
        now = _utcnow()
        stale_before = now - timedelta(
            seconds=get_settings().crawler_review_claim_seconds
        )
        claims: list[ReviewContentCleanupClaim] = []
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(CrawlJobRow)
                .where(CrawlJobRow.progress_json.is_not(None))
                .order_by(CrawlJobRow.updated_at.asc(), CrawlJobRow.id.asc())
                .with_for_update(skip_locked=True)
            )
            for row in result.scalars():
                progress = dict(row.progress_json or {})
                items = [dict(item) for item in progress.get("review_items") or []]
                changed = False
                for item in items:
                    if len(claims) >= max(1, limit):
                        break
                    if (
                        item.get("status") != "rejected"
                        or item.get("reviewer") != "agent"
                        or item.get("agent_decision") != "reject"
                    ):
                        continue
                    expires_at = _review_timestamp(
                        item.get("review_content_expires_at")
                    )
                    cleanup_pending = bool(
                        item.get("review_content_cleanup_pending")
                    )
                    if not cleanup_pending and (
                        not item.get("review_content_available")
                        or expires_at is None
                        or expires_at > now
                    ):
                        continue
                    claimed_at = _review_timestamp(
                        item.get("review_content_cleanup_claimed_at")
                    )
                    if (
                        item.get("review_content_cleanup_claim_token")
                        and claimed_at is not None
                        and claimed_at > stale_before
                    ):
                        continue
                    item_id = str(item.get("id") or "")
                    if not item_id:
                        continue
                    token = str(uuid4())
                    item["review_content_available"] = False
                    item["review_content_cleanup_pending"] = True
                    item["review_content_cleanup_claim_token"] = token
                    item["review_content_cleanup_claimed_at"] = now.isoformat()
                    claims.append(
                        ReviewContentCleanupClaim(
                            crawl_job_id=row.id,
                            item_id=item_id,
                            claim_token=token,
                        )
                    )
                    changed = True
                if changed:
                    progress["review_items"] = items
                    row.progress_json = progress
                    row.updated_at = now
                if len(claims) >= max(1, limit):
                    break
            await session.commit()
        return claims

    async def finalize_review_content_cleanup(
        self,
        claim: ReviewContentCleanupClaim,
    ) -> bool:
        return await self._update_review_content_cleanup_claim(
            claim,
            values={
                "review_content_cleanup_pending": False,
                "review_content_cleanup_claim_token": None,
                "review_content_cleanup_claimed_at": None,
                "review_content_expired_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def release_review_content_cleanup(
        self,
        claim: ReviewContentCleanupClaim,
    ) -> bool:
        return await self._update_review_content_cleanup_claim(
            claim,
            values={
                "review_content_cleanup_claim_token": None,
                "review_content_cleanup_claimed_at": None,
            },
        )

    async def _update_review_content_cleanup_claim(
        self,
        claim: ReviewContentCleanupClaim,
        *,
        values: dict[str, Any],
    ) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(
                CrawlJobRow,
                claim.crawl_job_id,
                with_for_update=True,
            )
            if row is None:
                return False
            progress = dict(row.progress_json or {})
            items = [dict(item) for item in progress.get("review_items") or []]
            item = next(
                (
                    candidate
                    for candidate in items
                    if candidate.get("id") == claim.item_id
                ),
                None,
            )
            if item is None or (
                item.get("review_content_cleanup_claim_token")
                != claim.claim_token
            ):
                return False
            item.update(values)
            progress["review_items"] = items
            row.progress_json = progress
            row.updated_at = _utcnow()
            await session.commit()
            return True

    async def update_review_item(
        self,
        job_id: str,
        item_id: str,
        *,
        expected_statuses: set[str],
        values: dict[str, Any],
        ingest_job_id: str | None = None,
        increment_queued: bool = False,
        expected_claim_token: str | None = None,
    ) -> bool:
        """Update one review item without replacing concurrent item changes."""
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler job not found")
            progress = dict(row.progress_json or {})
            items = [dict(item) for item in progress.get("review_items") or []]
            item = next(
                (candidate for candidate in items if candidate.get("id") == item_id),
                None,
            )
            if item is None:
                raise LookupError("Review item not found")
            if str(item.get("status") or "") not in expected_statuses:
                return False
            if expected_claim_token is not None and (
                item.get("review_claim_token") != expected_claim_token
            ):
                return False
            item.update(values)
            if increment_queued:
                progress["queued_for_ingest"] = int(
                    progress.get("queued_for_ingest", 0)
                ) + 1
            progress["review_items"] = items
            if ingest_job_id:
                ingest_ids = list(row.ingest_job_ids_json or [])
                if ingest_job_id not in ingest_ids:
                    ingest_ids.append(ingest_job_id)
                row.ingest_job_ids_json = ingest_ids
            _refresh_review_counts(progress)
            row.progress_json = progress
            row.updated_at = _utcnow()
            await session.commit()
            return True

    async def patch_review_progress(
        self,
        job_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError("Crawler job not found")
            progress = dict(row.progress_json or {})
            progress.update(values)
            _refresh_review_counts(progress)
            row.progress_json = progress
            row.updated_at = _utcnow()
            await session.commit()
            return progress

    async def finish(
        self,
        job_id: str,
        status: CrawlJobStatus,
        *,
        progress: dict[str, Any] | None = None,
        error_message: str | None = None,
        expected_attempt: int | None = None,
    ) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(CrawlJobRow, job_id, with_for_update=True)
            if row is None:
                return False
            if expected_attempt is not None and (
                row.attempt != expected_attempt
                or row.status != CrawlJobStatus.RUNNING
            ):
                return False
            row.status = status
            if progress is not None:
                row.progress_json = progress
            row.error_message = error_message[:4000] if error_message else None
            row.finished_at = None if status == CrawlJobStatus.PAUSED else _utcnow()
            row.updated_at = _utcnow()
            await session.commit()
            return True

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

    async def request_cancel_for_source(self, source_id: str) -> list[CrawlJobRow]:
        """Cancel every active run belonging to one managed crawler source."""
        active_statuses = {
            CrawlJobStatus.QUEUED,
            CrawlJobStatus.RUNNING,
            CrawlJobStatus.PAUSED,
            CrawlJobStatus.FAILED,
        }
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            result = await session.execute(
                select(CrawlJobRow)
                .join(
                    CrawlerSourceRunRow,
                    CrawlerSourceRunRow.crawl_job_id == CrawlJobRow.id,
                )
                .where(
                    CrawlerSourceRunRow.source_id == source_id,
                    CrawlJobRow.status.in_(active_statuses),
                )
                .with_for_update()
            )
            rows = list(result.scalars().unique().all())
            now = _utcnow()
            for row in rows:
                row.cancel_requested = True
                if row.status in {
                    CrawlJobStatus.QUEUED,
                    CrawlJobStatus.PAUSED,
                    CrawlJobStatus.FAILED,
                }:
                    row.status = CrawlJobStatus.CANCELLED
                    row.finished_at = now
                row.updated_at = now
            await session.commit()
            return rows

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
    ) -> bool:
        async with AsyncSession(get_engine()) as session:
            knowledge_base = (
                await session.execute(
                    select(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == knowledge_base_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if knowledge_base is None:
                return False
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
            return True

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
