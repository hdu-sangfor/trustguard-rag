"""从已提交抽取文本重新分块，并重建 MySQL、Qdrant 和 OpenSearch 数据。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.core.embedding.client import EmbeddingClient, normalize_embedding_provider
from app.core.indexing.opensearch_indexer import OpenSearchIndexer
from app.core.indexing.qdrant_indexer import QdrantIndexer
from app.core.ingest.chunker import chunk_extracted_text
from app.core.retrieval.keyword_retriever import KeywordRetriever
from app.domain import DocumentStatus
from app.settings import get_settings
from app.stores import db, opensearch_store, qdrant_store
from app.stores.blob_store import get_blob_store
from app.stores.chunk_store import ChunkStore
from app.stores.document_store import DocumentStore


@dataclass(frozen=True)
class RechunkResult:
    documents: int
    chunks: int
    prompt_tokens: int
    total_tokens: int


@dataclass
class _PreparedDocument:
    document: Any
    chunks: list[dict[str, Any]]
    vectors: list[list[float]]


def _chunk_id(document_id: str, chunk_index: int) -> str:
    """生成与正常入库流程一致的确定性分块 ID。"""
    return str(uuid5(NAMESPACE_URL, f"trustguard:document:{document_id}:chunk:{chunk_index}"))


async def _prepare_documents() -> tuple[list[_PreparedDocument], int, int]:
    """在修改任一存储前完成文本读取、分块和向量生成。"""
    settings = get_settings()
    documents = await DocumentStore().list_by_status(DocumentStatus.READY)
    blobs = get_blob_store()
    embedder = EmbeddingClient(settings)
    provider = normalize_embedding_provider(settings.embedding_provider)
    prepared: list[_PreparedDocument] = []
    prompt_tokens = 0
    total_tokens = 0

    for document in documents:
        if not document.blob_path:
            raise RuntimeError(f"文档 {document.id} 缺少 blob_path，无法重新分块")
        artifact_path = f"{document.blob_path.rstrip('/')}/extracted.txt"
        if not blobs.exists(artifact_path):
            raise RuntimeError(f"文档 {document.id} 缺少抽取文本 {artifact_path}")
        drafts = chunk_extracted_text(blobs.read_text(artifact_path))
        if not drafts:
            raise RuntimeError(f"文档 {document.id} 未生成任何分块")
        embedding_result = await embedder.embed_texts_with_usage([draft.text for draft in drafts])
        if embedding_result.usage is not None:
            prompt_tokens += embedding_result.usage.prompt_tokens
            total_tokens += embedding_result.usage.total_tokens
        chunks: list[dict[str, Any]] = []
        for index, draft in enumerate(drafts):
            chunk_id = _chunk_id(document.id, index)
            chunks.append(
                {
                    "id": chunk_id,
                    "document_id": document.id,
                    "chunk_index": index,
                    "text": draft.text,
                    "token_count": draft.token_count,
                    "page_no": draft.page_no,
                    "embedding_model": settings.embedding_model,
                    "embedding_dim": settings.embedding_dim,
                    "qdrant_point_id": chunk_id,
                    "metadata": {
                        **draft.metadata,
                        "embedding_provider": provider,
                    },
                }
            )
        prepared.append(
            _PreparedDocument(
                document=document,
                chunks=chunks,
                vectors=embedding_result.vectors,
            )
        )
    return prepared, prompt_tokens, total_tokens


async def rechunk_search_indexes() -> RechunkResult:
    """维护窗口内重新分块，并以新数据整体替换三个存储中的旧分块。"""
    prepared, prompt_tokens, total_tokens = await _prepare_documents()

    qdrant = qdrant_store.get_client()
    qdrant_indexer = QdrantIndexer()
    collections = {item.name for item in (await qdrant.get_collections()).collections}
    if qdrant_indexer.collection_name in collections:
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

    await ChunkStore().replace_for_documents({item.document.id: item.chunks for item in prepared})
    return RechunkResult(
        documents=len(prepared),
        chunks=sum(len(item.chunks) for item in prepared),
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
    )


async def _main() -> None:
    try:
        result = await rechunk_search_indexes()
        print(json.dumps(asdict(result), ensure_ascii=False))
    finally:
        await opensearch_store.close()
        await qdrant_store.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
