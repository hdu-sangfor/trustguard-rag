"""Ingest HTTP API."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from app.core.ingest.pipeline import get_ingest_pipeline
from app.schemas.ingest import ConflictResolveRequest, IngestJobCreateResponse, IngestJobResponse
from app.stores.blob_store import get_blob_store
from app.stores.job_store import get_job_store
from app.workers.run_ingest_job import enqueue_ingest_job

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])


def _job_response(job) -> IngestJobResponse:
    return IngestJobResponse(
        id=job.id,
        source_type=job.source_type,
        status=job.status,
        current_step=job.current_step,
        document_id=job.document_id,
        pending_document_id=job.pending_document_id,
        conflict_candidates=list(job.conflict_candidates_json or []),
        error_code=job.error_code,
        error_message=job.error_message,
        step_logs=list(job.step_logs or []),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.post("/jobs", response_model=IngestJobCreateResponse)
async def create_ingest_job(
    background_tasks: BackgroundTasks,
    source_type: str = Form(...),
    file: UploadFile = File(...),
) -> IngestJobCreateResponse:
    if source_type != "file":
        raise HTTPException(status_code=400, detail="Only source_type=file is supported")
    js = get_job_store()
    bs = get_blob_store()
    data = await file.read()
    original_filename = file.filename or "upload.bin"
    mime = file.content_type
    job = await js.create(
        source_type=source_type,
        source=original_filename,
        options={"original_filename": original_filename, "mime": mime},
    )
    bs.put_job_upload(job.id, data)
    await enqueue_ingest_job(background_tasks, job.id)
    return IngestJobCreateResponse(job_id=job.id, status=job.status)


@router.get("/jobs/{job_id}", response_model=IngestJobResponse)
async def get_ingest_job(job_id: str) -> IngestJobResponse:
    job = await get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)


@router.post("/jobs/{job_id}/resolve", response_model=IngestJobResponse)
async def resolve_conflict(job_id: str, body: ConflictResolveRequest) -> IngestJobResponse:
    pl = get_ingest_pipeline()
    try:
        await pl.resolve_conflict(job_id, body.keep_document_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    job = await get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)
