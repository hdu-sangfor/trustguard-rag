"""增量同步 HTTP API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.core.ingest.source_uri import SourceUriError
from app.core.ingest.sync import get_sync_runner
from app.schemas.sync import CursorResponse, SyncRequest, SyncResponse
from app.stores.cursor_store import get_cursor_store

router = APIRouter(prefix="/v1/ingest", tags=["ingest-sync"])


@router.post(
    "/sync",
    response_model=SyncResponse,
    status_code=status.HTTP_200_OK,
)
async def run_sync(body: SyncRequest) -> SyncResponse:
    """批量/目录增量同步：SKIP / ADD / UPDATE / 可选 cleanup=full。"""
    try:
        return await get_sync_runner().run(body)
    except SourceUriError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/cursors/{cursor_key}", response_model=CursorResponse)
async def get_cursor(cursor_key: str) -> CursorResponse:
    """读取增量同步水位。"""
    if not cursor_key or len(cursor_key) > 64:
        raise HTTPException(status_code=400, detail="Invalid cursor_key")
    value = await get_cursor_store().get(cursor_key)
    return CursorResponse(cursor_key=cursor_key, cursor_value=value)
