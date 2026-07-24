"""OpenSearch BM25 关键词检索器。"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from app.core.retrieval.filters import build_opensearch_filters, matches_filters
from app.core.retrieval.security_entities import (
    build_security_entity_fields,
    exact_entity_match_priority,
    extract_security_entity_ids,
)
from app.settings import get_settings
from app.stores import opensearch_store

_INDEX_INIT_LOCK = asyncio.Lock()
_SECURITY_ENTITY_PROPERTIES = {
    "entity_id": {"type": "keyword"},
    "entity_type": {"type": "keyword"},
    "entity_ids": {"type": "keyword"},
    "entity_types": {"type": "keyword"},
    "title": {"type": "text", "analyzer": "standard"},
    "aliases": {"type": "keyword"},
}


class KeywordRetriever:
    """基于 OpenSearch BM25 全文检索，返回带分数的结果。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._index = f"{self._settings.opensearch_index_prefix}chunks"

    @property
    def index_name(self) -> str:
        return self._index

    async def ensure_index(self) -> bool:
        """确保业务索引存在，并返回本次调用是否创建了索引。"""
        client = opensearch_store.get_client()
        exists = await client.indices.exists(index=self._index)
        if exists:
            await client.indices.put_mapping(
                index=self._index,
                body={
                    "properties": {
                        "knowledge_base_id": {"type": "keyword"},
                        **_SECURITY_ENTITY_PROPERTIES,
                    }
                },
            )
            return False
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
                    "dynamic_templates": [
                        {
                            "metadata_strings": {
                                "path_match": "metadata.*",
                                "match_mapping_type": "string",
                                "mapping": {"type": "keyword"},
                            }
                        }
                    ],
                    "properties": {
                        "chunk_id": {"type": "keyword"},
                        "knowledge_base_id": {"type": "keyword"},
                        **_SECURITY_ENTITY_PROPERTIES,
                        "document_id": {"type": "keyword"},
                        "chunk_index": {"type": "integer"},
                        "text": {"type": "text", "analyzer": "standard"},
                        "source_uri": {"type": "keyword"},
                        "original_filename": {"type": "keyword"},
                        "page_no": {"type": "integer"},
                        "metadata": {"type": "object", "dynamic": True},
                    }
                },
            },
        )
        return True

    async def index_chunk(
        self,
        chunk_id: str,
        text: str,
        document_id: str,
        chunk_index: int,
        source_uri: str,
        original_filename: str | None,
        page_no: int | None,
        knowledge_base_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        security_fields: dict[str, Any] | None = None,
    ) -> None:
        client = opensearch_store.get_client()
        security_fields = security_fields or build_security_entity_fields(
            text=text,
            original_filename=original_filename,
            metadata=metadata,
        )
        await client.index(
            index=self._index,
            id=chunk_id,
            body={
                "chunk_id": chunk_id,
                "document_id": document_id,
                "knowledge_base_id": knowledge_base_id,
                "chunk_index": chunk_index,
                "text": text,
                "source_uri": source_uri,
                "original_filename": original_filename,
                **security_fields,
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
            response = await client.bulk(body=body, refresh=True)
            if response.get("errors"):
                failed = [
                    item
                    for item in response.get("items", [])
                    if item.get("index", {}).get("error")
                ]
                raise RuntimeError(f"OpenSearch bulk indexing failed for {len(failed)} chunks")

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or self._settings.search_keyword_top_k

        filter_clauses = build_opensearch_filters(filters)
        query_entity_ids = extract_security_entity_ids(query)
        if query_entity_ids:
            retrieval_query: dict[str, Any] = {
                "bool": {
                    "should": [
                        {
                            "terms": {
                                "entity_id": query_entity_ids,
                                "boost": 100.0,
                            }
                        },
                        {
                            "terms": {
                                "entity_ids": query_entity_ids,
                                "boost": 40.0,
                            }
                        },
                        {
                            "terms": {
                                "aliases": query_entity_ids,
                                "boost": 20.0,
                            }
                        },
                        {
                            "match": {
                                "title": {
                                    "query": query,
                                    "boost": 5.0,
                                }
                            }
                        },
                        {"match": {"text": {"query": query}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        else:
            retrieval_query = {
                "multi_match": {
                    "query": query,
                    "fields": ["title^3", "text"],
                }
            }

        # 仅确保索引存在；全量回填留给 startup / 运维入口，避免读路径阻塞。
        async with _INDEX_INIT_LOCK:
            await self.ensure_index()

        client = opensearch_store.get_client()
        response = await client.search(
            index=self._index,
            body={
                "query": {
                    "bool": {
                        "must": [retrieval_query],
                        "filter": filter_clauses,
                    }
                },
                "size": top_k,
            },
        )

        results = []
        for hit in response["hits"]["hits"]:
            src = hit["_source"]
            results.append(
                {
                    "chunk_id": hit["_id"],
                    "text": src.get("text", ""),
                    "score": float(hit["_score"]),
                    "document_id": src.get("document_id"),
                    "knowledge_base_id": src.get("knowledge_base_id"),
                    "chunk_index": src.get("chunk_index"),
                    "page_no": src.get("page_no"),
                    "source_uri": src.get("source_uri"),
                    "original_filename": src.get("original_filename"),
                    "entity_id": src.get("entity_id"),
                    "entity_type": src.get("entity_type"),
                    "entity_ids": src.get("entity_ids") or [],
                    "entity_types": src.get("entity_types") or [],
                    "title": src.get("title"),
                    "aliases": src.get("aliases") or [],
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
    """模拟模式下返回空结果。"""

    async def ensure_index(self) -> bool:
        return False

    async def index_chunk(
        self,
        chunk_id: str,
        text: str,
        document_id: str,
        chunk_index: int,
        source_uri: str,
        original_filename: str | None,
        page_no: int | None,
        knowledge_base_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        security_fields: dict[str, Any] | None = None,
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
    """基于查询词命中率计算模拟 TF-IDF 分数（种子跨进程稳定）。"""
    if not query or not text:
        return 0.0
    query_terms = set(query.lower().split())
    text_lower = text.lower()
    hits = sum(1 for term in query_terms if term in text_lower)
    if hits == 0:
        return 0.0
    digest = hashlib.sha256(f"{text}\0{query}".encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big") % 100
    return hits * 0.3 + (seed / 200.0) + 0.1


class PseudoKeywordRetriever:
    """基于原始文本模拟 BM25，无外部依赖，用于开发和测试场景。"""

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
        knowledge_base_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        security_fields: dict[str, Any] | None = None,
    ) -> None:
        security_fields = security_fields or build_security_entity_fields(
            text=text,
            original_filename=original_filename,
            metadata=metadata,
        )
        self._chunks[chunk_id] = {
            "chunk_id": chunk_id,
            "text": text,
            "document_id": document_id,
            "knowledge_base_id": knowledge_base_id,
            "chunk_index": chunk_index,
            "source_uri": source_uri,
            "original_filename": original_filename,
            **security_fields,
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
        query_entity_ids = extract_security_entity_ids(query)
        scored = []
        for cid, info in self._chunks.items():
            if not matches_filters(info, filters):
                continue
            exact_priority = exact_entity_match_priority(info, query_entity_ids)
            score = _fake_score_from_text(info["text"], query) + exact_priority * 100.0
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
