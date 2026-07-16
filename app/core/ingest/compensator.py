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


class CleanupError(RuntimeError):
    """一个或多个外部索引清理操作失败，允许后续幂等重试。"""

    def __init__(self, failures: list[str]) -> None:
        self.failures = tuple(failures)
        super().__init__(f"cleanup failed for: {', '.join(failures)}")


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

    async def _delete_search_indexes(
        self, document_id: str, point_ids: list[str]
    ) -> list[str]:
        """独立尝试双删，避免一个后端失败阻止另一个后端清理。"""
        failures: list[str] = []
        try:
            await self._delete_vectors(document_id, point_ids)
        except Exception:  # noqa: BLE001
            failures.append("qdrant")
            logger.warning("failed to delete qdrant points for %s", document_id, exc_info=True)
        try:
            await self._opensearch_indexer.delete_for_document(document_id)
        except Exception:  # noqa: BLE001
            failures.append("opensearch")
            logger.warning("failed to delete opensearch docs for %s", document_id, exc_info=True)
        return failures

    def _delete_artifacts(self, document_id: str, doc) -> None:
        """幂等删除文档 artifact 前缀。"""
        if doc and doc.blob_path:
            self._blobs.delete_prefix(doc.blob_path)
        else:
            self._blobs.delete_prefix(f"artifacts/{document_id}")

    async def rollback_document(self, document_id: str) -> bool:
        """删除发布失败文档的 artifacts、分块和向量。"""
        doc = await self._documents.get(document_id)
        if not doc:
            return True
        point_ids = await self._chunks.point_ids_for_document(document_id)
        failures = await self._delete_search_indexes(document_id, point_ids)

        try:
            self._delete_artifacts(document_id, doc)
        except Exception:  # noqa: BLE001
            failures.append("artifacts")
            logger.warning("failed to delete artifacts for %s", document_id, exc_info=True)

        try:
            await self._chunks.delete_for_document(document_id)
        except Exception:  # noqa: BLE001
            failures.append("chunks")
            logger.warning("failed to delete chunks for %s", document_id, exc_info=True)

        await self._documents.update_status(document_id, DocumentStatus.FAILED)
        return not failures

    async def supersede_document(self, document_id: str) -> None:
        """在冲突胜出文档确定后删除旧的已发布文档。"""
        doc = await self._documents.get(document_id)
        if not doc:
            return
        if doc.status == DocumentStatus.SUPERSEDED:
            return
        if doc.status != DocumentStatus.SUPERSEDING:
            await self._documents.update_status(document_id, DocumentStatus.SUPERSEDING)
        point_ids = await self._chunks.point_ids_for_document(document_id)
        failures = await self._delete_search_indexes(document_id, point_ids)
        if failures:
            raise CleanupError(failures)

        self._delete_artifacts(document_id, doc)
        await self._chunks.delete_for_document(document_id)
        await self._documents.update_status(document_id, DocumentStatus.SUPERSEDED)

    async def delete_document(self, document_id: str) -> bool:
        """删除文档的向量、artifacts、分块和元数据。"""
        doc = await self._documents.get(document_id)
        if not doc:
            return False

        if doc.status != DocumentStatus.DELETING:
            await self._documents.update_status(document_id, DocumentStatus.DELETING)
        point_ids = await self._chunks.point_ids_for_document(document_id)
        failures = await self._delete_search_indexes(document_id, point_ids)
        if failures:
            raise CleanupError(failures)

        self._delete_artifacts(document_id, doc)
        await self._chunks.delete_for_document(document_id)
        await self._jobs.clear_document_references(document_id)
        return await self._documents.delete(document_id)

    async def resume_pending_cleanups(self) -> dict[str, int]:
        """启动时续跑持久化的删除、替换和失败发布补偿。"""
        resumed = {"deleting": 0, "superseeding": 0, "failed": 0, "errors": 0}
        for doc in await self._documents.list_by_status(DocumentStatus.DELETING):
            try:
                await self.delete_document(doc.id)
                resumed["deleting"] += 1
            except Exception:  # noqa: BLE001
                resumed["errors"] += 1
                logger.warning("failed to resume deletion for %s", doc.id, exc_info=True)
        for doc in await self._documents.list_by_status(DocumentStatus.SUPERSEDING):
            try:
                await self.supersede_document(doc.id)
                resumed["superseeding"] += 1
            except Exception:  # noqa: BLE001
                resumed["errors"] += 1
                logger.warning("failed to resume supersede for %s", doc.id, exc_info=True)
        for doc in await self._documents.list_by_status(DocumentStatus.FAILED):
            try:
                if await self.rollback_document(doc.id):
                    resumed["failed"] += 1
                else:
                    resumed["errors"] += 1
            except Exception:  # noqa: BLE001
                resumed["errors"] += 1
                logger.warning("failed to resume rollback for %s", doc.id, exc_info=True)
        return resumed


def get_compensator() -> Compensator:
    """使用已配置的存储和索引器创建补偿器。"""
    return Compensator()
