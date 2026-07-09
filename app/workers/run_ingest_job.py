"""后台入库任务入口。"""
from __future__ import annotations

import logging

from fastapi import BackgroundTasks

from app.core.ingest.pipeline import get_ingest_pipeline

logger = logging.getLogger(__name__)


async def run_ingest_job(job_id: str) -> None:
    """在后台任务中执行一个入库任务。"""
    pipeline = get_ingest_pipeline()
    await pipeline.run(job_id)


async def enqueue_ingest_job(background_tasks: BackgroundTasks, job_id: str) -> None:
    """将入库任务注册到 FastAPI 进程内后台任务执行器。"""
    background_tasks.add_task(run_ingest_job, job_id)
