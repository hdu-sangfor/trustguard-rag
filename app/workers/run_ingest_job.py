"""Background ingest worker entrypoint."""
from __future__ import annotations

import logging

from fastapi import BackgroundTasks

from app.core.ingest.pipeline import get_ingest_pipeline

logger = logging.getLogger(__name__)


async def run_ingest_job(job_id: str) -> None:
    pipeline = get_ingest_pipeline()
    await pipeline.run(job_id)


async def enqueue_ingest_job(background_tasks: BackgroundTasks, job_id: str) -> None:
    background_tasks.add_task(run_ingest_job, job_id)
