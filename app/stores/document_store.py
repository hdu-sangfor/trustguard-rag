"""Document metadata store."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import DocumentRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DocumentStore:
    async def create(
        self,
        *,
        source_type: str,
        source_uri: str,
        content_hash: str,
        status: str = "staging",
        title: str | None = None,
        mime_type: str | None = None,
        original_filename: str | None = None,
        doc_version: int = 1,
        blob_path: str | None = None,
        metadata: dict[str, Any] | None = None,
        document_id: str | None = None,
    ) -> DocumentRow:
        row = DocumentRow(
            id=document_id or str(uuid4()),
            source_type=source_type,
            source_uri=source_uri,
            content_hash=content_hash,
            title=title,
            mime_type=mime_type,
            original_filename=original_filename,
            doc_version=doc_version,
            status=status,
            blob_path=blob_path,
            metadata_json=metadata,
        )
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get(self, document_id: str) -> DocumentRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(DocumentRow, document_id)

    async def find_by_source(
        self, source_type: str, source_uri: str, content_hash: str
    ) -> DocumentRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(DocumentRow).where(
                    DocumentRow.source_type == source_type,
                    DocumentRow.source_uri == source_uri,
                    DocumentRow.content_hash == content_hash,
                )
            )
            return result.scalar_one_or_none()

    async def find_ready_by_filename(self, original_filename: str) -> list[DocumentRow]:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(DocumentRow).where(
                    DocumentRow.original_filename == original_filename,
                    DocumentRow.status == "ready",
                )
            )
            return list(result.scalars().all())

    async def find_ready_by_source_uri(
        self, source_uri: str, exclude_hash: str | None = None
    ) -> list[DocumentRow]:
        async with AsyncSession(get_engine()) as session:
            q = select(DocumentRow).where(
                DocumentRow.source_uri == source_uri,
                DocumentRow.status == "ready",
            )
            if exclude_hash:
                q = q.where(DocumentRow.content_hash != exclude_hash)
            result = await session.execute(q)
            return list(result.scalars().all())

    async def update_status(
        self,
        document_id: str,
        status: str,
        *,
        blob_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status, "updated_at": _utcnow()}
        if blob_path is not None:
            values["blob_path"] = blob_path
        if metadata is not None:
            values["metadata_json"] = metadata
        async with AsyncSession(get_engine()) as session:
            await session.execute(
                update(DocumentRow).where(DocumentRow.id == document_id).values(**values)
            )
            await session.commit()

    async def list_by_status(self, status: str) -> list[DocumentRow]:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(select(DocumentRow).where(DocumentRow.status == status))
            return list(result.scalars().all())


def get_document_store() -> DocumentStore:
    return DocumentStore()
