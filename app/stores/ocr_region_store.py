"""OCR 区域持久化。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ocr.protocol import OcrRegionDraft
from app.domain import OcrRegionStatus
from app.stores.blob_store import BlobStore, get_blob_store
from app.stores.db import get_engine
from app.stores.models import OcrRegionRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OcrRegionStore:
    def __init__(self, blob_store: BlobStore | None = None) -> None:
        self._blobs = blob_store or get_blob_store()

    async def create_from_drafts(
        self,
        document_id: str,
        drafts: list[OcrRegionDraft],
        *,
        version: int = 1,
    ) -> list[OcrRegionRow]:
        if not drafts:
            return []
        rows: list[OcrRegionRow] = []
        async with AsyncSession(get_engine()) as session:
            for draft in drafts:
                rid = str(uuid4())
                crop_rel = None
                if draft.crop_png:
                    crop_rel = self._blobs.write_artifact_file(
                        document_id,
                        version=version,
                        relative_name=f"ocr/{rid}.png",
                        data=draft.crop_png,
                    )
                try:
                    status = OcrRegionStatus(draft.status)
                except ValueError:
                    status = OcrRegionStatus.PENDING
                row = OcrRegionRow(
                    id=rid,
                    document_id=document_id,
                    page_no=draft.page_no,
                    bbox_json=list(draft.bbox),
                    crop_blob_path=crop_rel,
                    ocr_text=draft.ocr_text or "",
                    corrected_text=None,
                    status=status,
                    provider=draft.provider,
                    confidence=draft.confidence,
                    error_message=draft.error_message,
                    metadata_json=draft.metadata or None,
                )
                session.add(row)
                rows.append(row)
            await session.commit()
            for row in rows:
                await session.refresh(row)
        return rows

    async def list_for_document(
        self,
        document_id: str,
        *,
        status: OcrRegionStatus | None = None,
    ) -> list[OcrRegionRow]:
        async with AsyncSession(get_engine()) as session:
            q = select(OcrRegionRow).where(OcrRegionRow.document_id == document_id)
            if status is not None:
                q = q.where(OcrRegionRow.status == status)
            q = q.order_by(OcrRegionRow.page_no.asc(), OcrRegionRow.id.asc())
            result = await session.execute(q)
            return list(result.scalars().all())

    async def get(self, region_id: str) -> OcrRegionRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(OcrRegionRow, region_id)

    async def review(
        self,
        region_id: str,
        *,
        action: str,
        corrected_text: str | None = None,
    ) -> OcrRegionRow | None:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(OcrRegionRow, region_id)
            if not row:
                return None
            if action == "approve":
                row.status = OcrRegionStatus.APPROVED
            elif action == "correct":
                if corrected_text is None:
                    raise ValueError("corrected_text is required for correct action")
                row.corrected_text = corrected_text
                row.status = OcrRegionStatus.CORRECTED
            else:
                raise ValueError(f"unsupported review action: {action}")
            row.updated_at = _utcnow()
            await session.commit()
            await session.refresh(row)
            return row

    async def effective_texts_for_document(self, document_id: str) -> list[dict[str, Any]]:
        """返回用于重拼 extracted 文本的区域有效文字。"""
        rows = await self.list_for_document(document_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            text = row.corrected_text if row.status == OcrRegionStatus.CORRECTED else row.ocr_text
            out.append(
                {
                    "id": row.id,
                    "page_no": row.page_no,
                    "text": text or "",
                    "status": row.status.value if hasattr(row.status, "value") else str(row.status),
                }
            )
        return out


def get_ocr_region_store() -> OcrRegionStore:
    return OcrRegionStore()
