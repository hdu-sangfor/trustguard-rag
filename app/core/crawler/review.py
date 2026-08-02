"""Human and Agent review gate for cleaned crawler output."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from weakref import WeakValueDictionary

from minio.error import S3Error

from app.settings import get_settings
from app.stores.blob_store import get_blob_store
from app.stores.crawler_store import CrawlerStore
from app.stores.document_store import get_document_store
from app.stores.job_store import JobStore
from app.workers.eager import dispatch_eager
from app.workers.messages import REVIEWED_INGEST_DOCUMENT

_REVIEW_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_MISSING_BLOB_ERROR_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})


def _parse_review_timestamp(value: Any) -> datetime | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _review_content_expired(item: dict[str, Any]) -> bool:
    expires_at = _parse_review_timestamp(item.get("review_content_expires_at"))
    return expires_at is not None and expires_at <= datetime.now(timezone.utc)


def _review_content_available(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if status == "rejected":
        return bool(item.get("review_content_available")) and not _review_content_expired(
            item
        )
    return status in {"pending", "processing", "rejecting", "approved"}


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    public = {
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
            "agent_decision",
            "manual_reviewer",
            "manual_reviewed_at",
            "rejected_at",
            "review_content_expires_at",
            "review_content_expired_at",
        )
    }
    public["review_content_available"] = _review_content_available(item)
    return public


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


def _is_missing_blob_error(error: Exception) -> bool:
    return isinstance(error, FileNotFoundError) or (
        isinstance(error, S3Error) and error.code in _MISSING_BLOB_ERROR_CODES
    )


async def _read_committed_review_content(
    item: dict[str, Any],
    blobs: Any,
) -> str | None:
    if item.get("status") != "approved" or not item.get("ingest_job_id"):
        return None

    ingest_job = await JobStore().get(str(item["ingest_job_id"]))
    if ingest_job is None or not ingest_job.document_id:
        return None

    document = await get_document_store().get(str(ingest_job.document_id))
    if document is None or str(document.knowledge_base_id) != str(
        item.get("knowledge_base_id")
    ):
        return None

    blob_path = document.blob_path or f"artifacts/{document.id}/v{document.doc_version}"
    try:
        return blobs.read_text(f"{blob_path.rstrip('/')}/extracted.txt")
    except Exception as error:
        if _is_missing_blob_error(error):
            return None
        raise


async def get_review_content(job_id: str, item_id: str) -> dict[str, Any]:
    row = await CrawlerStore().get(job_id)
    if row is None:
        raise LookupError("Crawler job not found")
    items = [dict(item) for item in (row.progress_json or {}).get("review_items") or []]
    item = next((candidate for candidate in items if candidate.get("id") == item_id), None)
    if item is None:
        raise LookupError("Review item not found")
    if item.get("status") == "rejected" and not _review_content_available(item):
        raise LookupError("Review content retention period has expired")
    blobs = get_blob_store()
    try:
        content = blobs.read_job_upload(item_id).decode("utf-8")
    except Exception as error:
        if not _is_missing_blob_error(error):
            raise
        content = await _read_committed_review_content(item, blobs)
        if content is None:
            raise LookupError("Review content is no longer available") from error
    return {"item": _public_item(item), "content": content}


async def apply_review(
    job_id: str,
    *,
    action: str,
    item_ids: list[str],
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Serialize same-job updates locally; database row locks cover other workers."""
    lock = _REVIEW_LOCKS.setdefault(job_id, asyncio.Lock())
    async with lock:
        return await _apply_review(
            job_id,
            action=action,
            item_ids=item_ids,
            reviewer=reviewer,
        )


async def _apply_review(
    job_id: str,
    *,
    action: str,
    item_ids: list[str],
    reviewer: str | None,
) -> dict[str, Any]:
    store = CrawlerStore()
    claimed = await store.claim_review_items(
        job_id,
        action=action,
        item_ids=item_ids,
        allow_rejected_approval=bool(reviewer),
    )
    blob_store = get_blob_store()
    jobs = JobStore()
    for item in claimed:
        item_id = str(item["id"])
        claim_token = str(item["review_claim_token"])
        transition_status = "processing" if action == "approve" else "rejecting"
        previous_status = str(item.get("review_previous_status") or "pending")
        reviewed_at = datetime.now(timezone.utc).isoformat()
        manual_review_values = (
            {
                "manual_reviewer": reviewer,
                "manual_reviewed_at": reviewed_at,
            }
            if reviewer
            else {}
        )
        try:
            if action == "reject":
                retain_agent_rejection = (
                    reviewer is None
                    and item.get("reviewer") == "agent"
                    and item.get("agent_decision") == "reject"
                )
                if retain_agent_rejection:
                    expires_at = datetime.now(timezone.utc) + timedelta(
                        days=get_settings().crawler_agent_rejection_retention_days
                    )
                    retention_values = {
                        "review_content_available": True,
                        "review_content_expires_at": expires_at.isoformat(),
                        "review_content_expired_at": None,
                        "review_content_cleanup_pending": False,
                    }
                else:
                    blob_store.delete_job_staging(item_id)
                    retention_values = {
                        "review_content_available": False,
                        "review_content_expires_at": None,
                        "review_content_expired_at": reviewed_at,
                        "review_content_cleanup_pending": False,
                    }
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
                        "rejected_at": reviewed_at,
                        **retention_values,
                        **manual_review_values,
                        "review_claim_token": None,
                        "review_claimed_at": None,
                        "review_previous_status": None,
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
                        "reviewed_by": str(
                            reviewer or item.get("reviewer") or "human"
                        ),
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
                    "review_content_available": True,
                    "review_content_expires_at": None,
                    "review_content_expired_at": None,
                    "review_content_cleanup_pending": False,
                    **manual_review_values,
                    "review_claim_token": None,
                    "review_claimed_at": None,
                    "review_previous_status": None,
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
                    "status": previous_status,
                    "review_claim_token": None,
                    "review_claimed_at": None,
                    "review_previous_status": None,
                },
            )
            raise

    row = await store.get(job_id)
    if row is None:
        raise LookupError("Crawler job not found")
    return review_response(job_id, dict(row.progress_json or {}))
