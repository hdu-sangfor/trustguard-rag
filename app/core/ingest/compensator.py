"""Publication rollback / compensation."""
from __future__ import annotations

import logging

from app.core.indexing.qdrant_indexer import get_qdrant_indexer
from app.stores.blob_store import BlobStore, get_blob_store
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore

logger = logging.getLogger(__name__)


class Compensator:
    def __init__(
        self,
        *,
        document_store: DocumentStore | None = None,
        chunk_store: ChunkStore | None = None,
        blob_store: BlobStore | None = None,
        indexer=None,
    ) -> None:
        self._documents = document_store or DocumentStore()
        self._chunks = chunk_store or ChunkStore()
        self._blobs = blob_store or get_blob_store()
        self._indexer = indexer or get_qdrant_indexer()

    async def rollback_document(self, document_id: str) -> None:
        doc = await self._documents.get(document_id)
        if doc and doc.blob_path:
            self._blobs.delete_prefix(doc.blob_path)
        else:
            self._blobs.delete_prefix(f"artifacts/{document_id}")

        point_ids = await self._chunks.delete_for_document(document_id)
        if point_ids:
            try:
                await self._indexer.delete_points(point_ids)
            except Exception:
                logger.warning("failed to delete qdrant points for %s", document_id, exc_info=True)

        await self._documents.update_status(document_id, "failed")

    async def supersede_document(self, document_id: str) -> None:
        doc = await self._documents.get(document_id)
        if doc and doc.blob_path:
            self._blobs.delete_prefix(doc.blob_path)
        else:
            self._blobs.delete_prefix(f"artifacts/{document_id}")

        point_ids = await self._chunks.delete_for_document(document_id)
        if point_ids:
            try:
                await self._indexer.delete_points(point_ids)
            except Exception:
                logger.warning("failed to delete qdrant points for %s", document_id, exc_info=True)

        await self._documents.update_status(document_id, "superseded")


def get_compensator() -> Compensator:
    return Compensator()
