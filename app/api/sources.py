"""数据源能力 API。"""
from __future__ import annotations

from fastapi import APIRouter

from app.settings import get_settings

router = APIRouter(prefix="/v1/sources", tags=["sources"])


@router.get("/capabilities")
async def source_capabilities() -> dict:
    """描述入库 API 支持的数据源类型和上传限制。"""
    settings = get_settings()
    return {
        "sources": [
            {
                "source_type": "file",
                "mime_types": [
                    "application/pdf",
                    "text/plain",
                    "text/markdown",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ],
                "max_bytes": settings.ingest_max_pdf_bytes,
                "max_pdf_pages": settings.ingest_max_pdf_pages,
                "parsers": {
                    "application/pdf": "mineru",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "mineru",
                    "text/plain": "utf8-passthrough",
                    "text/markdown": "utf8-passthrough",
                },
            }
        ]
    }
