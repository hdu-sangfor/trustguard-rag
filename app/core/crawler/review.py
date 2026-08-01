"""Human and Agent review gate for cleaned crawler output."""

from __future__ import annotations

import asyncio
from typing import Any
from weakref import WeakValueDictionary

from app.stores.blob_store import get_blob_store
from app.stores.crawler_store import CrawlerStore
from app.stores.job_store import JobStore
from app.workers.eager import dispatch_eager
from app.workers.messages import REVIEWED_INGEST_DOCUMENT

_REVIEW_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "status",
            "knowledge_base_id",
            "title",
            "source_uri",
            "source_type",
            "original_filename",
            "content_preview",
            "content_chars",
            "created_at",
            "ingest_job_id",
            "reviewer",
            "review_reason",
            "review_confidence",
        )
    }


def review_response(job_id: str, progress: dict[str, Any]) -> dict[str, Any]:
    items = [dict(item) for item in progress.get("review_items") or []]
    pending = sum(
        item.get("status") in {"pending", "processing", "rejecting"}
        for item in items
    )
    approved = sum(item.get("status") == "approved" for item in items)
    rejected = sum(item.get("status") == "rejected" for item in items)
    return {
        "job_id": job_id,
        "review_status": str(progress.get("review_status") or ("pending" if pending else "completed")),
        "review_mode": str(progress.get("review_mode") or "human"),
        "review_criteria": str(progress.get("review_criteria") or ""),
        "items": [_public_item(item) for item in items],
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
    }


async def get_review(job_id: str) -> dict[str, Any]:
    row = await CrawlerStore().get(job_id)
    if row is None:
        raise LookupError("Crawler job not found")
    return review_response(job_id, dict(row.progress_json or {}))


async def get_review_content(job_id: str, item_id: str) -> dict[str, Any]:
    row = await CrawlerStore().get(job_id)
    if row is None:
        raise LookupError("Crawler job not found")
    items = [dict(item) for item in (row.progress_json or {}).get("review_items") or []]
    item = next((candidate for candidate in items if candidate.get("id") == item_id), None)
    if item is None:
        raise LookupError("Review item not found")
    try:
        content = get_blob_store().read_job_upload(item_id).decode("utf-8")
    except FileNotFoundError as error:
        raise LookupError("Review content is no longer available") from error
    return {"item": _public_item(item), "content": content}


async def apply_review(
    job_id: str,
    *,
    action: str,
    item_ids: list[str],
) -> dict[str, Any]:
    """Serialize same-job updates locally; database row locks cover other workers."""
    lock = _REVIEW_LOCKS.setdefault(job_id, asyncio.Lock())
    async with lock:
        return await _apply_review(
            job_id,
            action=action,
            item_ids=item_ids,
        )


async def _apply_review(
    job_id: str,
    *,
    action: str,
    item_ids: list[str],
) -> dict[str, Any]:
    store = CrawlerStore()
    claimed = await store.claim_review_items(
        job_id,
        action=action,
        item_ids=item_ids,
    )
    blob_store = get_blob_store()
    jobs = JobStore()
    for item in claimed:
        item_id = str(item["id"])
        claim_token = str(item["review_claim_token"])
        transition_status = "processing" if action == "approve" else "rejecting"
        try:
            if action == "reject":
                blob_store.delete_job_staging(item_id)
                await store.record_url(
                    knowledge_base_id=str(item["knowledge_base_id"]),
                    url=str(item["source_uri"]),
                    status="rejected_by_review",
                    content_hash=str(item.get("content_hash") or "") or None,
                )
                await store.update_review_item(
                    job_id,
                    item_id,
                    expected_statuses={"rejecting"},
                    expected_claim_token=claim_token,
                    values={
                        "status": "rejected",
                        "review_claim_token": None,
                        "review_claimed_at": None,
                    },
                )
                continue

            ingest_job = await jobs.get(item_id)
            if ingest_job is None:
                ingest_job, event = await jobs.create_ingest_command(
                    job_id=item_id,
                    source_type=str(item["source_type"]),
                    source=str(item["source_uri"]),
                    knowledge_base_id=str(item["knowledge_base_id"]),
                    options={
                        **dict(item.get("options") or {}),
                        "review_approved": True,
                        "reviewed_by": str(item.get("reviewer") or "human"),
                        "review_reason": str(item.get("review_reason") or ""),
                        "review_confidence": item.get("review_confidence"),
                        "reviewed_crawl_job_id": job_id,
                    },
                    event_type=REVIEWED_INGEST_DOCUMENT,
                )
                await dispatch_eager(event)
            await store.record_url(
                knowledge_base_id=str(item["knowledge_base_id"]),
                url=str(item["source_uri"]),
                status="queued_for_ingest",
                content_hash=str(item.get("content_hash") or "") or None,
                ingest_job_id=ingest_job.id,
            )
            await store.update_review_item(
                job_id,
                item_id,
                expected_statuses={"processing"},
                expected_claim_token=claim_token,
                values={
                    "status": "approved",
                    "ingest_job_id": ingest_job.id,
                    "review_claim_token": None,
                    "review_claimed_at": None,
                },
                ingest_job_id=ingest_job.id,
                increment_queued=True,
            )
        except Exception:
            await store.update_review_item(
                job_id,
                item_id,
                expected_statuses={transition_status},
                expected_claim_token=claim_token,
                values={
                    "status": "pending",
                    "review_claim_token": None,
                    "review_claimed_at": None,
                },
            )
            raise

    row = await store.get(job_id)
    if row is None:
        raise LookupError("Crawler job not found")
    return review_response(job_id, dict(row.progress_json or {}))
