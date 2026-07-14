"""OpenSearch 文本索引器，用于 BM25 关键词检索引擎。"""
from __future__ import annotations

import logging
from typing import Any

from app.core.retrieval.keyword_retriever import (
    MockKeywordRetriever,
    PseudoKeywordRetriever,
    get_keyword_retriever,
)

logger = logging.getLogger(__name__)


class OpenSearchIndexer:
    """将分块文本索引入 OpenSearch 以支持 BM25 关键词检索。"""

    def __init__(self) -> None:
        self._retriever = get_keyword_retriever()

    async def ensure_index(self) -> None:
        await self._retriever.ensure_index()

    async def index_chunks(
        self,
        chunks: list[dict[str, Any]],
        *,
        source_uri: str,
        original_filename: str | None,
    ) -> None:
        if isinstance(self._retriever, (MockKeywordRetriever, PseudoKeywordRetriever)):
            for chunk in chunks:
                await self._retriever.index_chunk(
                    chunk_id=chunk["id"],
                    text=chunk["text"],
                    document_id=chunk["document_id"],
                    chunk_index=chunk["chunk_index"],
                    source_uri=source_uri,
                    original_filename=original_filename,
                    page_no=chunk.get("page_no"),
                    metadata=chunk.get("metadata"),
                )
            return

        bulk: list[dict[str, Any]] = []
        for chunk in chunks:
            bulk.append(
                {
                    "chunk_id": chunk["id"],
                    "body": {
                        "chunk_id": chunk["id"],
                        "document_id": chunk["document_id"],
                        "chunk_index": chunk["chunk_index"],
                        "text": chunk["text"],
                        "source_uri": source_uri,
                        "original_filename": original_filename,
                        "page_no": chunk.get("page_no"),
                        "metadata": chunk.get("metadata"),
                    },
                }
            )
        if bulk:
            await self._retriever.index_chunks_bulk(bulk)

    async def delete_for_document(self, document_id: str) -> None:
        await self._retriever.delete_for_document(document_id)


def get_opensearch_indexer() -> OpenSearchIndexer:
    return OpenSearchIndexer()
