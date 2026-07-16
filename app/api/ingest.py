"""入库 HTTP API。"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.schemas.ingest import ConflictResolveRequest, IngestJobCreateResponse, IngestJobResponse
from app.stores.blob_store import get_blob_store
from app.stores.job_store import get_job_store
from app.workers.eager import dispatch_eager

router = APIRouter(prefix="/v1/ingest", tags=["ingest"])


def _job_response(job) -> IngestJobResponse:
    """将数据库任务行映射为公开的入库任务响应结构。"""
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
        attempt=job.attempt or 0,
        max_attempts=job.max_attempts or 3,
        step_logs=list(job.step_logs or []),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@router.post(
    "/jobs",
    response_model=IngestJobCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_ingest_job(
    source_type: str = Form(...),
    file: UploadFile = File(...),
) -> IngestJobCreateResponse:
    """创建文件入库任务，保存上传文件，并加入后台执行队列。"""
    if source_type != "file":
        raise HTTPException(status_code=400, detail="Only source_type=file is supported")
    js = get_job_store()
    bs = get_blob_store()
    data = await file.read()
    original_filename = file.filename or "upload.bin"
    mime = file.content_type
    job_id = str(uuid4())
    bs.put_job_upload(job_id, data)
    try:
        job, event = await js.create_ingest_command(
            job_id=job_id,
            source_type=source_type,
            source=original_filename,
            options={"original_filename": original_filename, "mime": mime},
        )
    except Exception:
        bs.delete_job_staging(job_id)
        raise
    await dispatch_eager(event)
    return IngestJobCreateResponse(job_id=job.id, status=job.status)


@router.get("/jobs/{job_id}", response_model=IngestJobResponse)
async def get_ingest_job(job_id: str) -> IngestJobResponse:
    """按 ID 查询入库任务，不存在时返回 404。"""
    job = await get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)


@router.post(
    "/jobs/{job_id}/resolve",
    response_model=IngestJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resolve_conflict(job_id: str, body: ConflictResolveRequest) -> IngestJobResponse:
    """持久化冲突处理选择，并将异步解决命令加入队列。"""
    try:
        _, event = await get_job_store().request_resolution(job_id, body.keep_document_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await dispatch_eager(event)
    job = await get_job_store().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)
