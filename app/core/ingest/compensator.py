"""发布回滚与补偿逻辑。"""

from __future__ import annotations

import logging

from app.core.indexing.opensearch_indexer import get_opensearch_indexer
from app.core.indexing.qdrant_indexer import get_qdrant_indexer
from app.domain import DocumentStatus
from app.stores.blob_store import BlobStore, get_blob_store
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore

logger = logging.getLogger(__name__)


class Compensator:
    def __init__(
        self,
        *,
        document_store: DocumentStore | None = None,
        job_store: JobStore | None = None,
        chunk_store: ChunkStore | None = None,
        blob_store: BlobStore | None = None,
        indexer=None,
        opensearch_indexer=None,
    ) -> None:
        """组装用于撤销部分发布文档的存储和索引器。"""
        self._documents = document_store or DocumentStore()
        self._jobs = job_store or JobStore()
        self._chunks = chunk_store or ChunkStore()
        self._blobs = blob_store or get_blob_store()
        self._indexer = indexer or get_qdrant_indexer()
        self._opensearch_indexer = opensearch_indexer or get_opensearch_indexer()

    async def _delete_vectors(self, document_id: str, point_ids: list[str]) -> None:
        """同时按文档 payload 和已知 point ID 删除向量。"""
        await self._indexer.delete_document(document_id)
        if point_ids:
            await self._indexer.delete_points(point_ids)

    async def rollback_document(self, document_id: str) -> None:
        """删除发布失败文档的 artifacts、分块和向量。"""
        doc = await self._documents.get(document_id)
        point_ids = await self._chunks.point_ids_for_document(document_id)
        try:
            await self._delete_vectors(document_id, point_ids)
        except Exception:
            logger.warning("failed to delete qdrant points for %s", document_id, exc_info=True)

        try:
            if doc and doc.blob_path:
                self._blobs.delete_prefix(doc.blob_path)
            else:
                self._blobs.delete_prefix(f"artifacts/{document_id}")
        except Exception:
            logger.warning("failed to delete artifacts for %s", document_id, exc_info=True)

        try:
            await self._opensearch_indexer.delete_for_document(document_id)
        except Exception:
            logger.warning("failed to delete opensearch docs for %s", document_id, exc_info=True)

        try:
            await self._chunks.delete_for_document(document_id)
        except Exception:
            logger.warning("failed to delete chunks for %s", document_id, exc_info=True)

        await self._documents.update_status(document_id, DocumentStatus.FAILED)

    async def supersede_document(self, document_id: str) -> None:
        """在冲突胜出文档确定后删除旧的已发布文档。"""
        doc = await self._documents.get(document_id)
        point_ids = await self._chunks.point_ids_for_document(document_id)
        await self._delete_vectors(document_id, point_ids)
        await self._opensearch_indexer.delete_for_document(document_id)

        if doc and doc.blob_path:
            self._blobs.delete_prefix(doc.blob_path)
        else:
            self._blobs.delete_prefix(f"artifacts/{document_id}")

        await self._chunks.delete_for_document(document_id)
        await self._documents.update_status(document_id, DocumentStatus.SUPERSEDED)

    async def delete_document(self, document_id: str) -> bool:
        """删除文档的向量、artifacts、分块和元数据。"""
        doc = await self._documents.get(document_id)
        if not doc:
            return False

        point_ids = await self._chunks.point_ids_for_document(document_id)
        # CRUD 删除必须显式失败，不能静默留下孤儿索引数据。
        await self._delete_vectors(document_id, point_ids)
        await self._opensearch_indexer.delete_for_document(document_id)

        if doc.blob_path:
            self._blobs.delete_prefix(doc.blob_path)
        else:
            self._blobs.delete_prefix(f"artifacts/{document_id}")
        await self._chunks.delete_for_document(document_id)
        await self._jobs.clear_document_references(document_id)
        return await self._documents.delete(document_id)


def get_compensator() -> Compensator:
    """使用已配置的存储和索引器创建补偿器。"""
    return Compensator()
