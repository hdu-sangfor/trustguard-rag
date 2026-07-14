"""入库发布 Saga。"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from app.core.embedding.client import EmbeddingClient, normalize_embedding_provider
from app.core.indexing.opensearch_indexer import OpenSearchIndexer, get_opensearch_indexer
from app.core.indexing.qdrant_indexer import get_qdrant_indexer
from app.core.ingest.chunker import chunk_extracted_text
from app.core.ingest.compensator import Compensator
from app.core.ingest.errors import (
    ARTIFACT_WRITE_FAILED,
    FILENAME_CONFLICT,
    IngestError,
)
from app.core.ingest.extractors.file import FileExtractor
from app.core.ingest.models import ExtractedDocument
from app.domain import DocumentStatus
from app.settings import Settings, get_settings
from app.stores.blob_store import BlobStore, get_blob_store
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore

logger = logging.getLogger(__name__)


def _embedding_metadata(settings: Settings) -> dict[str, str]:
    """仅为本地嵌入记录模型下载源，避免远程/伪向量元数据产生歧义。"""
    provider = normalize_embedding_provider(settings.embedding_provider)
    metadata = {"embedding_provider": provider}
    if provider == "local":
        metadata["embedding_download_source"] = settings.embedding_download_source
    return metadata


class IngestPipeline:
    def __init__(
        self,
        *,
        job_store: JobStore | None = None,
        document_store: DocumentStore | None = None,
        chunk_store: ChunkStore | None = None,
        blob_store: BlobStore | None = None,
        extractor: FileExtractor | None = None,
        embedder: EmbeddingClient | None = None,
        indexer=None,
        compensator: Compensator | None = None,
        opensearch_indexer: OpenSearchIndexer | None = None,
    ) -> None:
        """组装流水线依赖，允许测试注入替身对象。"""
        self._jobs = job_store or JobStore()
        self._documents = document_store or DocumentStore()
        self._chunks = chunk_store or ChunkStore()
        self._blobs = blob_store or get_blob_store()
        self._extractor = extractor or FileExtractor()
        self._embedder = embedder or EmbeddingClient()
        self._indexer = indexer or get_qdrant_indexer()
        self._opensearch_indexer = opensearch_indexer or get_opensearch_indexer()
        self._compensator = compensator or Compensator(
            document_store=self._documents,
            job_store=self._jobs,
            chunk_store=self._chunks,
            blob_store=self._blobs,
            indexer=self._indexer,
            opensearch_indexer=self._opensearch_indexer,
        )

    async def run(self, job_id: str) -> None:
        """执行单个上传文件任务的完整入库流程。"""
        job = await self._jobs.get(job_id)
        if not job:
            return
        document_id: str | None = None
        try:
            await self._jobs.mark_running(job_id, "validate")
            file_bytes, original_filename, mime = await self._load_upload(job)
            settings = get_settings()
            if len(file_bytes) > settings.ingest_max_pdf_bytes:
                raise IngestError("FILE_TOO_LARGE", "File exceeds max size")

            await self._jobs.mark_running(job_id, "extract")
            extracted = self._extractor.extract(
                file_bytes, original_filename=original_filename, mime=mime
            )

            await self._jobs.mark_running(job_id, "dedup")
            existing = await self._documents.find_by_source(
                job.source_type, extracted.source_uri, extracted.content_hash
            )
            if existing and existing.status == DocumentStatus.READY:
                await self._jobs.finish(job_id, "deduplicated", document_id=existing.id)
                self._blobs.delete_job_staging(job_id)
                return

            await self._jobs.mark_running(job_id, "conflict_check")
            conflict_ids = await self._detect_conflicts(job, extracted)
            if conflict_ids:
                document_id = str(uuid4())
                pending = await self._documents.create(
                    source_type=job.source_type,
                    source_uri=extracted.source_uri,
                    content_hash=extracted.content_hash,
                    status=DocumentStatus.STAGING,
                    mime_type=extracted.mime,
                    original_filename=original_filename,
                    metadata=extracted.metadata,
                    document_id=document_id,
                )
                await self._jobs.finish(
                    job_id,
                    "conflict",
                    pending_document_id=pending.id,
                    conflict_candidates=conflict_ids,
                    error_code=FILENAME_CONFLICT,
                    error_message="Filename or source conflict detected",
                )
                return

            document_id = str(uuid4())
            await self._jobs.mark_running(job_id, "commit_artifacts")
            doc = await self._documents.create(
                source_type=job.source_type,
                source_uri=extracted.source_uri,
                content_hash=extracted.content_hash,
                status=DocumentStatus.INDEXING,
                mime_type=extracted.mime,
                original_filename=original_filename,
                metadata=extracted.metadata,
                document_id=document_id,
            )
            blob_path = await self._commit_artifacts(doc.id, extracted)
            await self._documents.update_status(
                doc.id, DocumentStatus.INDEXING, blob_path=blob_path
            )

            await self._jobs.mark_running(job_id, "chunk")
            drafts = chunk_extracted_text(extracted.text)
            if not drafts:
                raise IngestError("EMPTY_CONTENT", "No chunks produced")

            await self._jobs.mark_running(job_id, "embed")
            vectors = await self._embedder.embed_texts([d.text for d in drafts])
            settings = get_settings()
            embedding_metadata = _embedding_metadata(settings)

            chunk_rows: list[dict[str, Any]] = []
            for i, draft in enumerate(drafts):
                cid = str(uuid4())
                chunk_rows.append(
                    {
                        "id": cid,
                        "document_id": doc.id,
                        "chunk_index": i,
                        "text": draft.text,
                        "token_count": draft.token_count,
                        "page_no": draft.page_no,
                        "embedding_model": settings.embedding_model,
                        "embedding_dim": settings.embedding_dim,
                        "qdrant_point_id": cid,
                        "metadata": {
                            **draft.metadata,
                            **embedding_metadata,
                        },
                    }
                )

            await self._jobs.mark_running(job_id, "index")
            await self._indexer.upsert_chunks(
                document_id=doc.id,
                chunks=chunk_rows,
                vectors=vectors,
                source_uri=extracted.source_uri,
                original_filename=original_filename,
            )
            await self._chunks.create_many(chunk_rows)

            await self._jobs.mark_running(job_id, "opensearch_index")
            try:
                await self._opensearch_indexer.ensure_index()
                await self._opensearch_indexer.index_chunks(
                    chunk_rows,
                    source_uri=extracted.source_uri,
                    original_filename=original_filename,
                )
            except Exception:
                logger.warning(
                    "OpenSearch indexing failed for %s, continuing", doc.id, exc_info=True
                )

            await self._jobs.mark_running(job_id, "publish")
            await self._documents.update_status(doc.id, DocumentStatus.READY)
            await self._jobs.finish(job_id, "succeeded", document_id=doc.id)
            self._blobs.delete_job_staging(job_id)
        except IngestError as e:
            logger.warning("ingest job %s failed: %s", job_id, e.code)
            if document_id:
                await self._compensator.rollback_document(document_id)
            await self._jobs.finish(job_id, "failed", error_code=e.code, error_message=e.message)
            self._blobs.delete_job_staging(job_id)
        except Exception as e:
            logger.exception("ingest job %s unexpected error", job_id)
            if document_id:
                await self._compensator.rollback_document(document_id)
            await self._jobs.finish(job_id, "failed", error_code="INTERNAL", error_message=str(e))
            self._blobs.delete_job_staging(job_id)

    async def resolve_conflict(self, job_id: str, keep_document_id: str) -> None:
        """通过选择待定文档或已有文档来解决冲突任务。"""
        job = await self._jobs.get(job_id)
        if not job or job.status != "conflict":
            raise ValueError("Job is not in conflict state")
        pending_id = job.pending_document_id
        candidates = list(job.conflict_candidates_json or [])
        if not pending_id:
            raise ValueError("No pending document for conflict job")

        if keep_document_id == pending_id:
            for old_id in candidates:
                if old_id != pending_id:
                    await self._compensator.supersede_document(old_id)
            file_bytes, original_filename, mime = await self._load_upload(job)
            extracted = self._extractor.extract(
                file_bytes, original_filename=original_filename, mime=mime
            )
            await self._publish_pending(pending_id, extracted, original_filename, job_id)
        elif keep_document_id in candidates:
            await self._compensator.rollback_document(pending_id)
            await self._jobs.finish(job_id, "discarded", document_id=keep_document_id)
            self._blobs.delete_job_staging(job_id)
        else:
            raise ValueError("keep_document_id not in conflict set")

    async def _publish_pending(
        self,
        document_id: str,
        extracted: ExtractedDocument,
        original_filename: str,
        job_id: str,
    ) -> None:
        """按 artifact、分块、嵌入、索引步骤发布暂存的冲突胜出文档。"""
        try:
            await self._documents.update_status(document_id, DocumentStatus.INDEXING)
            blob_path = await self._commit_artifacts(document_id, extracted)
            await self._documents.update_status(
                document_id, DocumentStatus.INDEXING, blob_path=blob_path
            )
            drafts = chunk_extracted_text(extracted.text)
            vectors = await self._embedder.embed_texts([d.text for d in drafts])
            settings = get_settings()
            embedding_metadata = _embedding_metadata(settings)
            chunk_rows: list[dict[str, Any]] = []
            for i, draft in enumerate(drafts):
                cid = str(uuid4())
                chunk_rows.append(
                    {
                        "id": cid,
                        "document_id": document_id,
                        "chunk_index": i,
                        "text": draft.text,
                        "token_count": draft.token_count,
                        "page_no": draft.page_no,
                        "embedding_model": settings.embedding_model,
                        "embedding_dim": settings.embedding_dim,
                        "qdrant_point_id": cid,
                        "metadata": {
                            **draft.metadata,
                            **embedding_metadata,
                        },
                    }
                )
            await self._indexer.upsert_chunks(
                document_id=document_id,
                chunks=chunk_rows,
                vectors=vectors,
                source_uri=extracted.source_uri,
                original_filename=original_filename,
            )
            await self._chunks.create_many(chunk_rows)
            try:
                await self._opensearch_indexer.ensure_index()
                await self._opensearch_indexer.index_chunks(
                    chunk_rows,
                    source_uri=extracted.source_uri,
                    original_filename=original_filename,
                )
            except Exception:
                logger.warning(
                    "OpenSearch indexing failed for conflict resolve %s", document_id, exc_info=True
                )
            await self._documents.update_status(document_id, DocumentStatus.READY)
            await self._jobs.finish(job_id, "succeeded", document_id=document_id)
            self._blobs.delete_job_staging(job_id)
        except Exception:
            await self._compensator.rollback_document(document_id)
            raise

    async def _load_upload(self, job) -> tuple[bytes, str, str | None]:
        """加载任务暂存的上传字节和原始请求元数据。"""
        opts = job.options_json or {}
        original_filename = opts.get("original_filename", "upload.bin")
        mime = opts.get("mime")
        data = self._blobs.read_job_upload(job.id)
        return data, original_filename, mime

    async def _detect_conflicts(self, job, extracted: ExtractedDocument) -> list[str]:
        """查找按文件名或来源 URI 冲突的已发布文档。"""
        conflicts: list[str] = []
        if job.source_type == "file" and extracted.metadata.get("original_filename"):
            fname = extracted.metadata["original_filename"]
            for doc in await self._documents.find_ready_by_filename(fname):
                if doc.content_hash != extracted.content_hash:
                    conflicts.append(doc.id)
        for doc in await self._documents.find_ready_by_source_uri(
            extracted.source_uri, exclude_hash=extracted.content_hash
        ):
            if doc.id not in conflicts:
                conflicts.append(doc.id)
        if conflicts:
            return conflicts
        return []

    async def _commit_artifacts(self, document_id: str, extracted: ExtractedDocument) -> str:
        """将抽取文本、元数据和原始字节写入 blob 存储。"""
        meta = {
            "content_hash": extracted.content_hash,
            "mime": extracted.mime,
            "source_uri": extracted.source_uri,
            **extracted.metadata,
        }
        try:
            return self._blobs.commit_bundle(
                document_id,
                raw_name=extracted.raw_filename,
                raw_bytes=extracted.raw_bytes,
                extracted_text=extracted.text,
                meta=meta,
            )
        except Exception as e:
            raise IngestError(ARTIFACT_WRITE_FAILED, str(e)) from e


def get_ingest_pipeline() -> IngestPipeline:
    """使用生产配置的依赖创建入库流水线。"""
    return IngestPipeline()
