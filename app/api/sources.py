"""Source capabilities API."""
from __future__ import annotations

from fastapi import APIRouter

from app.settings import get_settings

router = APIRouter(prefix="/v1/sources", tags=["sources"])


@router.get("/capabilities")
async def source_capabilities() -> dict:
    settings = get_settings()
    return {
        "sources": [
            {
                "source_type": "file",
                "mime_types": ["application/pdf"],
                "max_bytes": settings.ingest_max_pdf_bytes,
                "max_pdf_pages": settings.ingest_max_pdf_pages,
            }
        ]
    }
