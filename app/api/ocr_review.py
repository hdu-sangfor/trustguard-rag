"""OCR 人工复核 API（仅后端，无前端）。"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.ingest.pipeline import get_ingest_pipeline
from app.domain import OcrRegionStatus
from app.stores.blob_store import get_blob_store
from app.stores.document_store import get_document_store
from app.stores.ocr_region_store import get_ocr_region_store

router = APIRouter(tags=["ocr-review"])


class OcrRegionResponse(BaseModel):
    id: str
    document_id: str
    page_no: int | None = None
    bbox: list[float] | None = None
    crop_blob_path: str | None = None
    ocr_text: str
    corrected_text: str | None = None
    status: str
    provider: str | None = None
    confidence: float | None = None
    error_message: str | None = None
    image_url: str | None = None


class OcrReviewRequest(BaseModel):
    action: Literal["approve", "correct"]
    corrected_text: str | None = Field(default=None)


def _region_response(row) -> OcrRegionResponse:
    return OcrRegionResponse(
        id=row.id,
        document_id=row.document_id,
        page_no=row.page_no,
        bbox=list(row.bbox_json) if row.bbox_json else None,
        crop_blob_path=row.crop_blob_path,
        ocr_text=row.ocr_text or "",
        corrected_text=row.corrected_text,
        status=row.status.value if hasattr(row.status, "value") else str(row.status),
        provider=row.provider,
        confidence=row.confidence,
        error_message=row.error_message,
        image_url=f"/v1/ocr-regions/{row.id}/image" if row.crop_blob_path else None,
    )


@router.get("/v1/documents/{document_id}/ocr-regions", response_model=list[OcrRegionResponse])
async def list_document_ocr_regions(
    document_id: str,
    status: str | None = None,
) -> list[OcrRegionResponse]:
    doc = await get_document_store().get(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="document not found")
    status_enum = None
    if status:
        try:
            status_enum = OcrRegionStatus(status)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid status: {status}") from e
    rows = await get_ocr_region_store().list_for_document(document_id, status=status_enum)
    return [_region_response(row) for row in rows]


@router.get("/v1/ocr-regions/{region_id}", response_model=OcrRegionResponse)
async def get_ocr_region(region_id: str) -> OcrRegionResponse:
    row = await get_ocr_region_store().get(region_id)
    if not row:
        raise HTTPException(status_code=404, detail="ocr region not found")
    return _region_response(row)


@router.get("/v1/ocr-regions/{region_id}/image")
async def get_ocr_region_image(region_id: str) -> Response:
    row = await get_ocr_region_store().get(region_id)
    if not row or not row.crop_blob_path:
        raise HTTPException(status_code=404, detail="ocr image not found")
    path = row.crop_blob_path.replace("\\", "/")
    expected_prefix = f"artifacts/{row.document_id}/"
    if ".." in path.split("/") or not path.startswith(expected_prefix) or "/ocr/" not in path:
        raise HTTPException(status_code=400, detail="invalid ocr image path")
    try:
        data = get_blob_store().read(row.crop_blob_path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="ocr image missing") from e
    return Response(content=data, media_type="image/png")


@router.post("/v1/ocr-regions/{region_id}/review", response_model=OcrRegionResponse)
async def review_ocr_region(region_id: str, body: OcrReviewRequest) -> OcrRegionResponse:
    store = get_ocr_region_store()
    try:
        row = await store.review(
            region_id,
            action=body.action,
            corrected_text=body.corrected_text,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not row:
        raise HTTPException(status_code=404, detail="ocr region not found")

    if body.action == "correct":
        try:
            await get_ingest_pipeline().republish_from_ocr_corrections(row.document_id)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"reindex failed: {e}") from e
        row = await store.get(region_id) or row

    return _region_response(row)
