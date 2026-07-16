"""入库发布 Saga。"""

from __future__ import annotations

import logging
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.core.embedding.client import EmbeddingClient, EmbeddingError, normalize_embedding_provider
from app.core.indexing.opensearch_indexer import OpenSearchIndexer, get_opensearch_indexer
from app.core.indexing.qdrant_indexer import get_qdrant_indexer
from app.core.ingest.chunker import chunk_extracted_text
from app.core.ingest.compensator import Compensator
from app.core.ingest.errors import (
    ARTIFACT_WRITE_FAILED,
    EMBEDDING_FAILED,
    EMPTY_CONTENT,
    FILE_TOO_LARGE,
    FILENAME_CONFLICT,
    INDEX_FAILED,
    INTERNAL,
    IngestError,
)
from app.core.ingest.extractors.file import FileExtractor
from app.core.ingest.models import ExtractedDocument
from app.domain import (
    CleanupAction,
    DocumentStatus,
    IngestJobStatus,
    IngestStep,
    PipelineResult,
)
from app.settings import Settings, get_settings
from app.stores.blob_store import BlobStore, get_blob_store
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore, LeaseLostError
from app.stores.outbox_store import OutboxStore
from app.workers.messages import CLEANUP_DOCUMENT

logger = logging.getLogger(__name__)

_RETRYABLE_INGEST_CODES = frozenset({INDEX_FAILED, ARTIFACT_WRITE_FAILED})


def _document_id_for_job(job_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"trustguard:ingest:{job_id}:document"))


def _chunk_id(document_id: str, chunk_index: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"trustguard:document:{document_id}:chunk:{chunk_index}"))


def _embedding_metadata(settings: Settings) -> dict[str, str]:
    """仅为本地嵌入记录模型下载源，避免远程/伪向量元数据产生歧义。"""
    provider = normalize_embedding_provider(settings.embedding_provider)
    metadata = {"embedding_provider": provider}
    if provider == "local":
        metadata["embedding_download_source"] = settings.embedding_download_source
    return metadata


async def _enqueue_cleanup(document_id: str, action: CleanupAction) -> None:
    """尽力立即调度；持久化的文档状态仍是故障恢复的依据。"""
    try:
        await OutboxStore().add(
            event_type=CLEANUP_DOCUMENT,
            aggregate_id=document_id,
            payload={"document_id": document_id, "action": action},
        )
    except Exception:  # noqa: BLE001
        logger.warning("failed to enqueue %s cleanup for %s", action, document_id, exc_info=True)


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

    async def run(
        self, job_id: str, *, lease_token: str | None = None
    ) -> PipelineResult:
        """执行单个上传文件任务的完整入库流程。"""
        job = await self._jobs.get(job_id)
        if not job:
            return PipelineResult.MISSING
        document_id: str | None = job.document_id
        try:
            await self._jobs.mark_running(
                job_id, IngestStep.RECOVER, lease_token=lease_token
            )
            if document_id:
                previous = await self._documents.get(document_id)
                if previous and previous.status == DocumentStatus.READY:
                    await self._jobs.finish(
                        job_id,
                        IngestJobStatus.SUCCEEDED,
                        document_id=document_id,
                        lease_token=lease_token,
                    )
                    self._blobs.delete_job_staging(job_id)
                    return PipelineResult.SUCCEEDED
                if previous and previous.status in {
                    DocumentStatus.STAGING,
                    DocumentStatus.INDEXING,
                    DocumentStatus.FAILED,
                }:
                    if not await self._compensator.rollback_document(document_id):
                        return await self._retry_or_fail(
                            job_id,
                            error_code=INDEX_FAILED,
                            error_message="Previous partial index cleanup is incomplete",
                            lease_token=lease_token,
                        )
                    await self._documents.delete(document_id)
                    await self._jobs.set_document_id(
                        job_id, None, lease_token=lease_token
                    )
                    document_id = None
            await self._jobs.mark_running(
                job_id, IngestStep.VALIDATE, lease_token=lease_token
            )
            file_bytes, original_filename, mime = await self._load_upload(job)
            settings = get_settings()
            if len(file_bytes) > settings.ingest_max_pdf_bytes:
                raise IngestError(FILE_TOO_LARGE, "File exceeds max size")

            await self._jobs.mark_running(
                job_id, IngestStep.EXTRACT, lease_token=lease_token
            )
            extracted = self._extractor.extract(
                file_bytes, original_filename=original_filename, mime=mime
            )

            await self._jobs.mark_running(
                job_id, IngestStep.DEDUP, lease_token=lease_token
            )
            existing = await self._documents.find_by_source(
                job.source_type, extracted.source_uri, extracted.content_hash
            )
            if existing and existing.status == DocumentStatus.READY:
                await self._jobs.finish(
                    job_id,
                    IngestJobStatus.DEDUPLICATED,
                    document_id=existing.id,
                    lease_token=lease_token,
                )
                self._blobs.delete_job_staging(job_id)
                return PipelineResult.DEDUPLICATED

            await self._jobs.mark_running(
                job_id, IngestStep.CONFLICT_CHECK, lease_token=lease_token
            )
            conflict_ids = await self._detect_conflicts(job, extracted)
            if conflict_ids:
                document_id = _document_id_for_job(job_id)
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
                    IngestJobStatus.CONFLICT,
                    pending_document_id=pending.id,
                    conflict_candidates=conflict_ids,
                    error_code=FILENAME_CONFLICT,
                    error_message="Filename or source conflict detected",
                    lease_token=lease_token,
                )
                return PipelineResult.CONFLICT

            document_id = _document_id_for_job(job_id)
            await self._jobs.mark_running(
                job_id, IngestStep.COMMIT_ARTIFACTS, lease_token=lease_token
            )
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
            await self._jobs.set_document_id(
                job_id, doc.id, lease_token=lease_token
            )
            blob_path = await self._commit_artifacts(doc.id, extracted)
            await self._documents.update_status(
                doc.id, DocumentStatus.INDEXING, blob_path=blob_path
            )

            await self._jobs.mark_running(
                job_id, IngestStep.CHUNK, lease_token=lease_token
            )
            drafts = chunk_extracted_text(extracted.text)
            if not drafts:
                raise IngestError(EMPTY_CONTENT, "No chunks produced")

            await self._jobs.mark_running(
                job_id, IngestStep.EMBED, lease_token=lease_token
            )
            vectors = await self._embedder.embed_texts([d.text for d in drafts])
            settings = get_settings()
            embedding_metadata = _embedding_metadata(settings)

            chunk_rows: list[dict[str, Any]] = []
            for i, draft in enumerate(drafts):
                cid = _chunk_id(doc.id, i)
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

            await self._jobs.mark_running(
                job_id, IngestStep.INDEX, lease_token=lease_token
            )
            await self._indexer.upsert_chunks(
                document_id=doc.id,
                chunks=chunk_rows,
                vectors=vectors,
                source_uri=extracted.source_uri,
                original_filename=original_filename,
            )
            await self._chunks.create_many(chunk_rows)

            await self._jobs.mark_running(
                job_id, IngestStep.OPENSEARCH_INDEX, lease_token=lease_token
            )
            await self._index_opensearch(
                chunk_rows,
                source_uri=extracted.source_uri,
                original_filename=original_filename,
            )

            await self._jobs.mark_running(
                job_id, IngestStep.PUBLISH, lease_token=lease_token
            )
            if lease_token is None:
                await self._documents.update_status(doc.id, DocumentStatus.READY)
                await self._jobs.finish(
                    job_id, IngestJobStatus.SUCCEEDED, document_id=doc.id
                )
            else:
                await self._jobs.publish_document(
                    job_id, doc.id, lease_token=lease_token
                )
            self._blobs.delete_job_staging(job_id)
            return PipelineResult.SUCCEEDED
        except LeaseLostError:
            logger.info("ingest job %s stopped after losing its lease", job_id)
            raise
        except IngestError as e:
            logger.warning("ingest job %s failed: %s", job_id, e.code)
            if document_id:
                await self._compensator.rollback_document(document_id)
            if e.code in _RETRYABLE_INGEST_CODES:
                return await self._retry_or_fail(
                    job_id,
                    error_code=e.code,
                    error_message=e.message,
                    lease_token=lease_token,
                )
            await self._jobs.finish(
                job_id,
                IngestJobStatus.FAILED,
                error_code=e.code,
                error_message=e.message,
                lease_token=lease_token,
            )
            self._blobs.delete_job_staging(job_id)
            return PipelineResult.FAILED
        except EmbeddingError as e:
            logger.warning("embedding failed for ingest job %s", job_id)
            if document_id:
                await self._compensator.rollback_document(document_id)
            if e.retryable:
                return await self._retry_or_fail(
                    job_id,
                    error_code=EMBEDDING_FAILED,
                    error_message=str(e),
                    lease_token=lease_token,
                )
            await self._jobs.finish(
                job_id,
                IngestJobStatus.FAILED,
                error_code=EMBEDDING_FAILED,
                error_message=str(e),
                lease_token=lease_token,
            )
            self._blobs.delete_job_staging(job_id)
            return PipelineResult.FAILED
        except Exception as e:
            logger.exception("ingest job %s unexpected error", job_id)
            if document_id:
                await self._compensator.rollback_document(document_id)
            return await self._retry_or_fail(
                job_id,
                error_code=INTERNAL,
                error_message=str(e),
                lease_token=lease_token,
            )

    async def resolve_conflict(
        self,
        job_id: str,
        keep_document_id: str,
        *,
        lease_token: str | None = None,
    ) -> PipelineResult:
        """通过选择待定文档或已有文档来解决冲突任务。"""
        job = await self._jobs.get(job_id)
        if not job or job.status not in {
            IngestJobStatus.CONFLICT,
            IngestJobStatus.RESOLVING,
            IngestJobStatus.RUNNING,
        }:
            raise ValueError("Job is not resolving a conflict")
        pending_id = job.pending_document_id
        candidates = list(job.conflict_candidates_json or [])
        if not pending_id:
            raise ValueError("No pending document for conflict job")
        await self._jobs.mark_running(
            job_id, IngestStep.RESOLVE, lease_token=lease_token
        )

        if keep_document_id == pending_id:
            file_bytes, original_filename, mime = await self._load_upload(job)
            extracted = self._extractor.extract(
                file_bytes, original_filename=original_filename, mime=mime
            )
            try:
                pending = await self._documents.get(pending_id)
                if pending and pending.status == DocumentStatus.FAILED:
                    if not await self._compensator.rollback_document(pending_id):
                        return await self._retry_or_fail(
                            job_id,
                            error_code=INDEX_FAILED,
                            error_message="Pending document cleanup is incomplete",
                            status=IngestJobStatus.RESOLVE_RETRYING,
                            lease_token=lease_token,
                        )
                await self._publish_pending(pending_id, extracted, original_filename)
                await self._jobs.mark_running(
                    job_id, IngestStep.RESOLVE_SUPERSEDE, lease_token=lease_token
                )
            except Exception as e:  # noqa: BLE001
                if isinstance(e, IngestError):
                    error_code, error_message = e.code, e.message
                else:
                    error_code, error_message = INTERNAL, str(e)
                retryable = not isinstance(e, IngestError) or error_code in _RETRYABLE_INGEST_CODES
                if retryable:
                    return await self._retry_or_fail(
                        job_id,
                        error_code=error_code,
                        error_message=error_message,
                        status=IngestJobStatus.RESOLVE_RETRYING,
                        lease_token=lease_token,
                    )
                await self._jobs.finish(
                    job_id,
                    IngestJobStatus.FAILED,
                    error_code=error_code,
                    error_message=error_message,
                    lease_token=lease_token,
                )
                self._blobs.delete_job_staging(job_id)
                return PipelineResult.FAILED

            cleanup_pending: list[str] = []
            for old_id in candidates:
                if old_id == pending_id:
                    continue
                try:
                    await self._jobs.mark_running(
                        job_id,
                        IngestStep.RESOLVE_SUPERSEDE,
                        lease_token=lease_token,
                    )
                    await self._compensator.supersede_document(old_id)
                except Exception:  # noqa: BLE001
                    cleanup_pending.append(old_id)
                    logger.warning("supersede cleanup pending for %s", old_id, exc_info=True)
                    await _enqueue_cleanup(old_id, CleanupAction.SUPERSEDE)
            if cleanup_pending:
                await self._jobs.append_step_log(
                    job_id,
                    IngestStep.SUPERSEDE_CLEANUP,
                    "pending",
                    detail=f"{len(cleanup_pending)} document(s) pending startup retry",
                )
            await self._jobs.mark_running(
                job_id, IngestStep.RESOLVE_PUBLISH, lease_token=lease_token
            )
            if lease_token is None:
                await self._documents.update_status(pending_id, DocumentStatus.READY)
                await self._jobs.finish(
                    job_id,
                    IngestJobStatus.SUCCEEDED,
                    document_id=pending_id,
                )
            else:
                await self._jobs.publish_document(
                    job_id, pending_id, lease_token=lease_token
                )
            self._blobs.delete_job_staging(job_id)
            return PipelineResult.SUCCEEDED
        elif keep_document_id in candidates:
            await self._jobs.mark_running(
                job_id, IngestStep.RESOLVE_DISCARD, lease_token=lease_token
            )
            cleaned = await self._compensator.rollback_document(pending_id)
            if not cleaned:
                await _enqueue_cleanup(pending_id, CleanupAction.ROLLBACK)
            await self._jobs.finish(
                job_id,
                IngestJobStatus.DISCARDED,
                document_id=keep_document_id,
                lease_token=lease_token,
            )
            self._blobs.delete_job_staging(job_id)
            return PipelineResult.DISCARDED
        else:
            raise ValueError("keep_document_id not in conflict set")

    async def _retry_or_fail(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        status: IngestJobStatus = IngestJobStatus.INGEST_RETRYING,
        lease_token: str | None = None,
    ) -> PipelineResult:
        """只要本次尝试尚未达到上限，就调度下一次投递。"""
        retrying = await self._jobs.mark_retrying(
            job_id,
            error_code=error_code,
            error_message=error_message,
            status=status,
            lease_token=lease_token,
        )
        if retrying:
            return PipelineResult.RETRYING
        self._blobs.delete_job_staging(job_id)
        return PipelineResult.FAILED

    async def _publish_pending(
        self,
        document_id: str,
        extracted: ExtractedDocument,
        original_filename: str,
    ) -> None:
        """按产物、分块、嵌入、索引步骤发布暂存的冲突胜出文档。"""
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
                cid = _chunk_id(document_id, i)
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
            await self._index_opensearch(
                chunk_rows,
                source_uri=extracted.source_uri,
                original_filename=original_filename,
            )
        except Exception:
            await self._compensator.rollback_document(document_id)
            raise

    async def _index_opensearch(
        self,
        chunks: list[dict[str, Any]],
        *,
        source_uri: str,
        original_filename: str | None,
    ) -> None:
        """把 OpenSearch 视为发布必需步骤，失败时触发 Saga 回滚。"""
        try:
            await self._opensearch_indexer.ensure_index()
            await self._opensearch_indexer.index_chunks(
                chunks,
                source_uri=source_uri,
                original_filename=original_filename,
            )
        except Exception as e:
            raise IngestError(INDEX_FAILED, "OpenSearch indexing failed") from e

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
        """将抽取文本、元数据和原始字节写入对象存储。"""
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
