"""入库任务存储。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import IngestJobRow


def _utcnow() -> datetime:
    """返回适用于 MySQL datetime 字段的无时区 UTC 时间。"""
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
        """创建 queued 状态的入库任务行，并初始化选项和日志。"""
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
        """按主键加载单个入库任务。"""
        async with AsyncSession(get_engine()) as session:
            return await session.get(IngestJobRow, job_id)

    async def mark_running(self, job_id: str, step: str) -> None:
        """将任务置为 running，并为当前步骤追加 started 日志。"""
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

    async def append_step_log(
        self,
        job_id: str,
        step: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        """追加步骤日志，不改变任务状态。"""
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
        """设置终态或冲突状态，并保存最终任务元数据。"""
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
            await session.execute(
                update(IngestJobRow).where(IngestJobRow.id == job_id).values(**values)
            )
            await session.commit()

    async def clear_document_references(self, document_id: str) -> None:
        """清空任务中的文档外键式引用，并从冲突候选中移除该文档。"""
        async with AsyncSession(get_engine()) as session:
            await session.execute(
                update(IngestJobRow)
                .where(IngestJobRow.document_id == document_id)
                .values(document_id=None)
            )
            await session.execute(
                update(IngestJobRow)
                .where(IngestJobRow.pending_document_id == document_id)
                .values(pending_document_id=None)
            )
            result = await session.execute(
                select(IngestJobRow).where(IngestJobRow.conflict_candidates_json.is_not(None))
            )
            for job in result.scalars():
                candidates = list(job.conflict_candidates_json or [])
                filtered = [candidate for candidate in candidates if candidate != document_id]
                if filtered != candidates:
                    job.conflict_candidates_json = filtered
            await session.commit()


def get_job_store() -> JobStore:
    """创建绑定共享数据库引擎的任务存储。"""
    return JobStore()
