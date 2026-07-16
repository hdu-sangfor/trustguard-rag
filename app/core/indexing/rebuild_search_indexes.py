"""从 MySQL 权威数据直接重建 Qdrant 和 OpenSearch 检索索引。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any

from app.core.embedding.client import EmbeddingClient, normalize_embedding_provider
from app.core.indexing.opensearch_indexer import OpenSearchIndexer
from app.core.indexing.qdrant_indexer import QdrantIndexer
from app.core.retrieval.keyword_retriever import KeywordRetriever
from app.domain import DocumentStatus
from app.settings import get_settings
from app.stores import db, opensearch_store, qdrant_store
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore


@dataclass(frozen=True)
class RebuildResult:
    documents: int
    chunks: int
    qdrant_points: int
    opensearch_documents: int


@dataclass
class _PreparedDocument:
    document: Any
    chunks: list[dict[str, Any]]
    vectors: list[list[float]]


async def rebuild_search_indexes() -> RebuildResult:
    """预先生成全部向量，成功后再替换两个索引中的历史数据。"""
    settings = get_settings()
    documents = await DocumentStore().list_by_status(DocumentStatus.READY)
    chunk_store = ChunkStore()
    embedder = EmbeddingClient(settings)
    provider = normalize_embedding_provider(settings.embedding_provider)
    prepared: list[_PreparedDocument] = []

    for document in documents:
        rows = await chunk_store.list_for_document(document.id)
        chunks = [
            {
                "id": row.id,
                "document_id": row.document_id,
                "chunk_index": row.chunk_index,
                "text": row.text,
                "page_no": row.page_no,
                "metadata": {
                    **(row.metadata_json or {}),
                    "embedding_provider": provider,
                },
            }
            for row in rows
            if row.status == "active"
        ]
        vectors = await embedder.embed_texts([chunk["text"] for chunk in chunks])
        prepared.append(_PreparedDocument(document=document, chunks=chunks, vectors=vectors))

    qdrant = qdrant_store.get_client()
    qdrant_indexer = QdrantIndexer()
    collection_names = {
        item.name for item in (await qdrant.get_collections()).collections
    }
    if qdrant_indexer.collection_name in collection_names:
        await qdrant.delete_collection(qdrant_indexer.collection_name)
    await qdrant_indexer.ensure_collection()

    for item in prepared:
        await qdrant_indexer.upsert_chunks(
            document_id=item.document.id,
            chunks=item.chunks,
            vectors=item.vectors,
            source_uri=item.document.source_uri,
            original_filename=item.document.original_filename,
        )

    opensearch = opensearch_store.get_client()
    keyword_retriever = KeywordRetriever()
    if await opensearch.indices.exists(index=keyword_retriever.index_name):
        await opensearch.indices.delete(index=keyword_retriever.index_name)
    await keyword_retriever.ensure_index()
    opensearch_indexer = OpenSearchIndexer(keyword_retriever)

    for item in prepared:
        await opensearch_indexer.index_chunks(
            item.chunks,
            source_uri=item.document.source_uri,
            original_filename=item.document.original_filename,
        )
        await chunk_store.update_embedding_configuration(
            [chunk["id"] for chunk in item.chunks],
            model=settings.embedding_model,
            dimension=settings.embedding_dim,
            provider=provider,
        )

    chunk_count = sum(len(item.chunks) for item in prepared)
    return RebuildResult(
        documents=len(prepared),
        chunks=chunk_count,
        qdrant_points=chunk_count,
        opensearch_documents=chunk_count,
    )


async def _main() -> None:
    try:
        result = await rebuild_search_indexes()
        print(json.dumps(asdict(result), ensure_ascii=False))
    finally:
        await opensearch_store.close()
        await qdrant_store.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
