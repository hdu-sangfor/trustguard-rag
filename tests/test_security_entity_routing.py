"""CVE/CWE/CAPEC 结构化字段与精确查询路由测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.retrieval.keyword_retriever import KeywordRetriever, PseudoKeywordRetriever
from app.core.retrieval.search import HybridSearch
from app.core.retrieval.security_entities import (
    build_security_entity_fields,
    extract_security_entity_ids,
)


def test_extract_security_entity_ids_normalizes_supported_identifiers() -> None:
    assert extract_security_entity_ids(
        "分析 cve-2026-12348、CWE:89 和 capec 100，并忽略 CVE-2026-12"
    ) == ["CVE-2026-12348", "CWE-89", "CAPEC-100"]


def test_security_fields_distinguish_primary_and_related_entities() -> None:
    fields = build_security_entity_fields(
        text="# 漏洞编号: CVE-2026-12348\n\n关联弱点 CWE-1021",
        original_filename="CVE-2026-12348.md",
        metadata={"aliases": ["Arc Android spoofing"]},
    )

    assert fields["entity_id"] == "CVE-2026-12348"
    assert fields["entity_type"] == "vulnerability"
    assert fields["entity_ids"] == ["CVE-2026-12348", "CWE-1021"]
    assert fields["entity_types"] == ["vulnerability", "weakness"]
    assert fields["title"] == "漏洞编号: CVE-2026-12348"
    assert fields["aliases"] == [
        "Arc Android spoofing",
        "CVE-2026-12348",
        "CWE-1021",
    ]


@pytest.mark.asyncio
async def test_pseudo_keyword_route_prioritizes_primary_exact_match(
    tmp_storage,
) -> None:
    retriever = PseudoKeywordRetriever()
    common = {
        "chunk_index": 0,
        "source_uri": "upload://security",
        "page_no": 1,
        "knowledge_base_id": "kb-1",
    }
    await retriever.index_chunk(
        chunk_id="primary",
        document_id="doc-primary",
        text="目标漏洞说明",
        original_filename="CVE-2026-12348.md",
        **common,
    )
    await retriever.index_chunk(
        chunk_id="related",
        document_id="doc-related",
        text="该漏洞与 CVE-2026-12348 存在关联",
        original_filename="CVE-2026-99999.md",
        **common,
    )

    results = await retriever.retrieve(
        "请分析 cve-2026-12348",
        filters={"knowledge_base_id": "kb-1"},
    )

    assert [item["chunk_id"] for item in results] == ["primary", "related"]
    assert results[0]["entity_id"] == "CVE-2026-12348"
    assert results[1]["entity_ids"] == ["CVE-2026-99999", "CVE-2026-12348"]


@pytest.mark.asyncio
async def test_opensearch_identifier_query_uses_exact_fields(
    monkeypatch, tmp_storage
) -> None:
    client = SimpleNamespace(
        indices=SimpleNamespace(
            exists=AsyncMock(return_value=True),
            put_mapping=AsyncMock(),
        ),
        search=AsyncMock(return_value={"hits": {"hits": []}}),
    )
    monkeypatch.setattr("app.stores.opensearch_store.get_client", lambda: client)

    await KeywordRetriever().retrieve(
        "CWE-89 的防御措施",
        filters={"knowledge_base_id": "kb-1"},
    )

    body = client.search.await_args.kwargs["body"]
    routed = body["query"]["bool"]["must"][0]["bool"]
    assert routed["minimum_should_match"] == 1
    assert routed["should"][0]["terms"]["entity_id"] == ["CWE-89"]
    assert body["query"]["bool"]["filter"] == [
        {"term": {"knowledge_base_id": "kb-1"}}
    ]


@pytest.mark.asyncio
async def test_hybrid_search_keeps_primary_exact_match_ahead_of_rerank(
    tmp_storage,
) -> None:
    engine = HybridSearch()
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-noise", "doc-target"})
    )
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "noise",
                    "document_id": "doc-noise",
                    "text": "其他安全内容",
                    "score": 0.99,
                    "original_filename": "overview.md",
                },
                {
                    "chunk_id": "target",
                    "document_id": "doc-target",
                    "text": "漏洞详情",
                    "score": 0.70,
                    "original_filename": "CVE-2026-12348.md",
                    "entity_id": "CVE-2026-12348",
                    "entity_ids": ["CVE-2026-12348"],
                },
            ]
        )
    )
    engine._reranker = SimpleNamespace(
        rerank=AsyncMock(
            side_effect=lambda _query, candidates, _top_k: list(
                reversed(candidates)
            )
        )
    )

    result = await engine.search(
        "分析 CVE-2026-12348",
        knowledge_base_id="kb-1",
        enable_keyword=False,
        enable_rerank=True,
    )

    assert result["query_entities"] == ["CVE-2026-12348"]
    assert result["results"][0]["chunk_id"] == "target"
    assert result["results"][0]["exact_entity_match"] == "primary"
    assert engine._vector.retrieve.await_args.args[2] == {
        "knowledge_base_id": "kb-1",
        "entity_ids_any": ["CVE-2026-12348"],
    }
    engine._documents.ready_ids.assert_awaited_once_with(
        ["doc-noise", "doc-target"],
        "kb-1",
    )


@pytest.mark.asyncio
async def test_exact_entity_prefilter_can_be_disabled(tmp_storage) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    engine._documents = SimpleNamespace(ready_ids=AsyncMock(return_value=set()))

    await engine.search(
        "CVE-2026-12348",
        knowledge_base_id="kb-1",
        enable_keyword=False,
        enable_rerank=False,
        require_exact_entity_match=False,
    )

    assert engine._vector.retrieve.await_args.args[2] == {
        "knowledge_base_id": "kb-1",
    }
