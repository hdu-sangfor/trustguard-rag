"""数据源能力 API。"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.embedding.profiles import list_embedding_profiles
from app.core.ingest.extractors.file import SUPPORTED_MIME_TYPES
from app.settings import get_settings

router = APIRouter(prefix="/v1/sources", tags=["sources"])


@router.get("/capabilities")
async def source_capabilities() -> dict:
    """描述入库 API 支持的数据源类型和上传限制。"""
    settings = get_settings()
    return {
        "embedding_profiles": [
            profile.public_dict(default=profile.id == "configured")
            for profile in list_embedding_profiles(settings)
        ],
        "sources": [
            {
                "source_type": "file",
                "mime_types": SUPPORTED_MIME_TYPES,
                "max_bytes": max(settings.ingest_max_pdf_bytes, settings.ingest_max_file_bytes),
                "max_pdf_pages": settings.ingest_max_pdf_pages,
                "parsers": {
                    "application/pdf": settings.pdf_parser,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "mineru",
                    "text/plain": "markitdown",
                    "text/markdown": "markitdown",
                    "text/csv": "markitdown",
                    "application/json": "markitdown",
                    "text/html": "markitdown",
                    "application/xhtml+xml": "markitdown",
                    "image/*": "ocr",
                },
                "ocr": {
                    "provider": settings.ocr_provider,
                    "api_driver": settings.ocr_api_driver,
                    "fail_open": settings.ocr_fail_open,
                },
            },
            {
                "source_type": "url",
                "enabled": settings.crawler_enabled,
                "managed_by": "/v1/crawler/jobs",
                "public_http_only": not settings.crawler_allow_private_urls,
                "max_total_pages": settings.crawler_max_total_pages,
                "parsers": {"text/html": "trafilatura"},
            },
        ]
    }
