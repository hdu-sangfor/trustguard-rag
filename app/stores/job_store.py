"""Ingest job store."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import IngestJobRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JobStore:
    async def create(
        self,
        *,
        source_type: str,
        source: str,
        options: dict[str, Any] | None = None,
        status: str = "queued",
    ) -> IngestJobRow:
        row = IngestJobRow(
            id=str(uuid4()),
            source_type=source_type,
            source=source,
            options_json=options,
            status=status,
            step_logs=[],
        )
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get(self, job_id: str) -> IngestJobRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(IngestJobRow, job_id)

    async def mark_running(self, job_id: str, step: str) -> None:
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id)
            if not job:
                return
            logs = list(job.step_logs or [])
            logs.append({"step": step, "status": "started", "at": _utcnow().isoformat()})
            job.status = "running"
            job.current_step = step
            job.started_at = job.started_at or _utcnow()
            job.attempt = (job.attempt or 0) + 1
            job.step_logs = logs
            await session.commit()

    async def append_step_log(self, job_id: str, step: str, status: str, detail: str | None = None) -> None:
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id)
            if not job:
                return
            logs = list(job.step_logs or [])
            entry: dict[str, Any] = {"step": step, "status": status, "at": _utcnow().isoformat()}
            if detail:
                entry["detail"] = detail
            logs.append(entry)
            job.step_logs = logs
            await session.commit()

    async def finish(
        self,
        job_id: str,
        status: str,
        *,
        document_id: str | None = None,
        pending_document_id: str | None = None,
        conflict_candidates: list[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "finished_at": _utcnow(),
        }
        if document_id is not None:
            values["document_id"] = document_id
        if pending_document_id is not None:
            values["pending_document_id"] = pending_document_id
        if conflict_candidates is not None:
            values["conflict_candidates_json"] = conflict_candidates
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message
        async with AsyncSession(get_engine()) as session:
            await session.execute(update(IngestJobRow).where(IngestJobRow.id == job_id).values(**values))
            await session.commit()


def get_job_store() -> JobStore:
    return JobStore()
