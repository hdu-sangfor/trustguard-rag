"""将 MySQL 中已发布的历史分块幂等回填到 OpenSearch。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.indexing.opensearch_indexer import OpenSearchIndexer
from app.core.retrieval.keyword_retriever import KeywordRetriever
from app.domain import DocumentStatus
from app.stores.chunk_store import ChunkStore, get_chunk_store
from app.stores.document_store import DocumentStore, get_document_store
from app.stores.knowledge_base_store import DEFAULT_KNOWLEDGE_BASE_ID


@dataclass(frozen=True)
class BackfillResult:
    documents: int = 0
    chunks: int = 0


async def backfill_ready_documents(
    *,
    retriever: KeywordRetriever | None = None,
    document_store: DocumentStore | None = None,
    chunk_store: ChunkStore | None = None,
    ensure_index: bool = True,
) -> BackfillResult:
    """按稳定分块 ID 重建就绪文档索引，可安全重复执行。"""
    keyword_retriever = retriever or KeywordRetriever()
    if ensure_index:
        await keyword_retriever.ensure_index()

    documents = await (document_store or get_document_store()).list_by_status(
        DocumentStatus.READY
    )
    chunks_source = chunk_store or get_chunk_store()
    indexer = OpenSearchIndexer(keyword_retriever)

    document_count = 0
    chunk_count = 0
    for document in documents:
        knowledge_base_id = getattr(
            document, "knowledge_base_id", None
        ) or DEFAULT_KNOWLEDGE_BASE_ID
        rows = await chunks_source.list_for_document(document.id)
        chunks: list[dict[str, Any]] = [
            {
                "id": row.id,
                "document_id": row.document_id,
                "knowledge_base_id": knowledge_base_id,
                "chunk_index": row.chunk_index,
                "text": row.text,
                "page_no": row.page_no,
                "metadata": {
                    **(row.metadata_json or {}),
                    "knowledge_base_id": knowledge_base_id,
                },
            }
            for row in rows
            if row.status == "active"
        ]
        if not chunks:
            continue
        await indexer.index_chunks(
            chunks,
            source_uri=document.source_uri,
            original_filename=document.original_filename,
        )
        document_count += 1
        chunk_count += len(chunks)

    return BackfillResult(documents=document_count, chunks=chunk_count)
