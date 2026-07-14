"""OpenSearch BM25 关键词检索器。"""

from __future__ import annotations

from typing import Any

from app.settings import get_settings
from app.stores import opensearch_store


class KeywordRetriever:
    """基于 OpenSearch BM25 全文检索，返回带分数的结果。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._index = f"{self._settings.opensearch_index_prefix}chunks"

    @property
    def index_name(self) -> str:
        return self._index

    async def ensure_index(self) -> None:
        client = opensearch_store.get_client()
        exists = await client.indices.exists(index=self._index)
        if not exists:
            await client.indices.create(
                index=self._index,
                body={
                    "settings": {
                        "index": {"number_of_shards": 1, "number_of_replicas": 0},
                        "analysis": {
                            "analyzer": {
                                "default": {"type": "standard"},
                            }
                        },
                    },
                    "mappings": {
                        "properties": {
                            "chunk_id": {"type": "keyword"},
                            "document_id": {"type": "keyword"},
                            "chunk_index": {"type": "integer"},
                            "text": {"type": "text", "analyzer": "standard"},
                            "source_uri": {"type": "keyword"},
                            "original_filename": {"type": "keyword"},
                            "page_no": {"type": "integer"},
                            "metadata": {"type": "object", "enabled": False},
                        }
                    },
                },
            )

    async def index_chunk(
        self,
        chunk_id: str,
        text: str,
        document_id: str,
        chunk_index: int,
        source_uri: str,
        original_filename: str | None,
        page_no: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        client = opensearch_store.get_client()
        await client.index(
            index=self._index,
            id=chunk_id,
            body={
                "chunk_id": chunk_id,
                "document_id": document_id,
                "chunk_index": chunk_index,
                "text": text,
                "source_uri": source_uri,
                "original_filename": original_filename,
                "page_no": page_no,
                "metadata": metadata or {},
            },
            refresh=True,
        )

    async def index_chunks_bulk(self, chunks: list[dict[str, Any]]) -> None:
        client = opensearch_store.get_client()
        body = []
        for c in chunks:
            body.append({"index": {"_index": self._index, "_id": c["chunk_id"]}})
            body.append(c["body"])
        if body:
            await client.bulk(body=body, refresh=True)

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or self._settings.search_keyword_top_k

        must_clauses = [{"match": {"text": query}}]
        if filters:
            for key, value in filters.items():
                must_clauses.append({"term": {key: value}})

        client = opensearch_store.get_client()
        try:
            response = await client.search(
                index=self._index,
                body={
                    "query": {"bool": {"must": must_clauses}},
                    "size": top_k,
                },
            )
        except Exception:
            return []

        results = []
        for hit in response["hits"]["hits"]:
            src = hit["_source"]
            results.append(
                {
                    "chunk_id": hit["_id"],
                    "text": src.get("text", ""),
                    "score": float(hit["_score"]),
                    "doc_id": src.get("document_id"),
                    "chunk_index": src.get("chunk_index"),
                    "page_no": src.get("page_no"),
                    "source_uri": src.get("source_uri"),
                    "original_filename": src.get("original_filename"),
                    "metadata": src.get("metadata"),
                }
            )
        return results

    async def delete_for_document(self, document_id: str) -> None:
        client = opensearch_store.get_client()
        if not await client.indices.exists(index=self._index):
            return
        await client.delete_by_query(
            index=self._index,
            body={"query": {"term": {"document_id": document_id}}},
            refresh=True,
        )


class MockKeywordRetriever:
    """mock 模式下返回空结果。"""

    async def ensure_index(self) -> None:
        pass

    async def index_chunk(
        self,
        chunk_id: str,
        text: str,
        document_id: str,
        chunk_index: int,
        source_uri: str,
        original_filename: str | None,
        page_no: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        pass

    async def index_chunks_bulk(self, chunks: list[dict[str, Any]]) -> None:
        pass

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def delete_for_document(self, document_id: str) -> None:
        pass


def _fake_score_from_text(text: str, query: str) -> float:
    """基于查询词命中率计算模拟 TF-IDF 分数。"""
    if not query or not text:
        return 0.0
    query_terms = set(query.lower().split())
    text_lower = text.lower()
    hits = sum(1 for term in query_terms if term in text_lower)
    if hits == 0:
        return 0.0
    seed = hash(text + query) % 100
    return hits * 0.3 + (seed / 200.0) + 0.1


class PseudoKeywordRetriever:
    """基于原始文本模拟 BM25，无外部依赖，用于 dev/test 场景。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._chunks: dict[str, dict[str, Any]] = {}

    async def ensure_index(self) -> None:
        pass

    async def index_chunk(
        self,
        chunk_id: str,
        text: str,
        document_id: str,
        chunk_index: int,
        source_uri: str,
        original_filename: str | None,
        page_no: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._chunks[chunk_id] = {
            "chunk_id": chunk_id,
            "text": text,
            "document_id": document_id,
            "chunk_index": chunk_index,
            "source_uri": source_uri,
            "original_filename": original_filename,
            "page_no": page_no,
            "metadata": metadata or {},
        }

    async def index_chunks_bulk(self, chunks: list[dict[str, Any]]) -> None:
        for c in chunks:
            self._chunks[c["chunk_id"]] = {**c["body"], "chunk_id": c["chunk_id"]}

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or self._settings.search_keyword_top_k
        scored = []
        for cid, info in self._chunks.items():
            if filters:
                match = all(info.get(k) == v for k, v in filters.items())
                if not match:
                    continue
            score = _fake_score_from_text(info["text"], query)
            if score > 0:
                scored.append((score, {**info, "chunk_id": cid, "score": score}))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:top_k]]

    async def delete_for_document(self, document_id: str) -> None:
        self._chunks = {
            k: v for k, v in self._chunks.items() if v.get("document_id") != document_id
        }


def get_keyword_retriever() -> KeywordRetriever | MockKeywordRetriever | PseudoKeywordRetriever:
    settings = get_settings()
    if settings.search_opensearch_mock:
        return PseudoKeywordRetriever()
    return KeywordRetriever()
