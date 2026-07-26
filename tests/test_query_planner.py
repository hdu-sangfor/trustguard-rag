"""查询规划、受控改写和动态检索预算测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.generation.llm_client import LLMCompletion, LLMError
from app.core.retrieval import query_planner as planner_module
from app.core.retrieval.query_planner import QueryPlanner
from app.core.retrieval.search import HybridSearch, _retrieve_query_variants
from app.domain import RetrievalMode
from app.settings import Settings


def _settings(**updates) -> Settings:
    values = {
        "llm_provider": "api",
        "llm_base_url": "https://example.invalid",
        "llm_api_key": "test",
        "query_planner_llm_enabled": True,
        "query_planner_cache_ttl_seconds": 0,
        **updates,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_enumeration_rule_uses_expanded_budget_without_llm() -> None:
    llm = SimpleNamespace(complete=AsyncMock())
    plan = await QueryPlanner(_settings(), llm).plan(
        "网络安全法包括哪些条款？"
    )

    assert plan.mode == RetrievalMode.ENUMERATION
    assert plan.source == "rule"
    assert plan.top_k == 20
    assert plan.vector_top_k == 80
    assert plan.keyword_top_k == 80
    assert plan.max_chunks_per_document == 10
    assert plan.rerank_candidate_top_k == 50
    assert plan.context_max_chunks == 20
    assert plan.coverage_status == "partial"
    llm.complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "两个漏洞哪个是反序列化，哪个是认证缺失？",
        "邮件到达和用户点开分别属于什么技术？",
        "怎样用日志验证从钓鱼到凭据转储的攻击链？",
    ],
)
async def test_multi_evidence_rules_select_comprehensive_mode(query: str) -> None:
    llm = SimpleNamespace(complete=AsyncMock())
    plan = await QueryPlanner(_settings(), llm).plan(query)

    assert plan.mode == RetrievalMode.COMPREHENSIVE
    assert plan.max_chunks_per_document == 5
    assert plan.adjacent_chunk_radius == 1
    llm.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_mode_and_limits_override_automatic_budget() -> None:
    plan = await QueryPlanner(
        _settings(llm_provider="none"),
        SimpleNamespace(complete=AsyncMock()),
    ).plan(
        "问题",
        requested_mode=RetrievalMode.COMPREHENSIVE,
        top_k=7,
        vector_top_k=11,
        keyword_top_k=12,
        max_chunks_per_document=2,
    )

    assert plan.source == "explicit"
    assert plan.mode == RetrievalMode.COMPREHENSIVE
    assert plan.top_k == 7
    assert plan.vector_top_k == 11
    assert plan.keyword_top_k == 12
    assert plan.max_chunks_per_document == 2


@pytest.mark.asyncio
async def test_ambiguous_query_uses_validated_llm_rewrite() -> None:
    llm = SimpleNamespace(
        complete=AsyncMock(
            return_value=LLMCompletion(
                content=(
                    '{"intent":"comprehensive","scope":"broad",'
                    '"target":"运营责任",'
                    '"semantic_queries":["网络运营者安全责任"],'
                    '"keyword_queries":["网络运营者 应当"],"confidence":0.91}'
                ),
                model="planner",
            )
        )
    )
    plan = await QueryPlanner(_settings(), llm).plan("谈谈运营者责任")

    assert plan.mode == RetrievalMode.COMPREHENSIVE
    assert plan.source == "llm"
    assert plan.semantic_queries == ("网络运营者安全责任",)
    assert plan.keyword_queries == ("网络运营者 应当",)
    llm.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_failure_and_low_confidence_fall_back_to_focused() -> None:
    failing = SimpleNamespace(
        complete=AsyncMock(side_effect=LLMError("unavailable"))
    )
    failed = await QueryPlanner(_settings(), failing).plan("模糊问题")
    assert failed.mode == RetrievalMode.FOCUSED
    assert failed.source == "fallback"

    low_confidence = SimpleNamespace(
        complete=AsyncMock(
            return_value=LLMCompletion(
                content=(
                    '{"intent":"enumeration","scope":"exhaustive",'
                    '"semantic_queries":[],"keyword_queries":[],"confidence":0.2}'
                ),
                model="planner",
            )
        )
    )
    low = await QueryPlanner(_settings(), low_confidence).plan("另一个模糊问题")
    assert low.mode == RetrievalMode.FOCUSED
    assert low.source == "fallback"


@pytest.mark.asyncio
async def test_planner_timeout_falls_back() -> None:
    async def slow_complete(_messages):
        await asyncio.sleep(0.05)

    plan = await QueryPlanner(
        _settings(query_planner_timeout_seconds=0.001),
        SimpleNamespace(complete=slow_complete),
    ).plan("需要规划的问题")
    assert plan.source == "fallback"


@pytest.mark.asyncio
async def test_successful_llm_plan_is_cached() -> None:
    planner_module._INTENT_CACHE.clear()
    llm = SimpleNamespace(
        complete=AsyncMock(
            return_value=LLMCompletion(
                content=(
                    '{"intent":"comprehensive","scope":"broad",'
                    '"semantic_queries":[],"keyword_queries":[],"confidence":0.9}'
                ),
                model="planner",
            )
        )
    )
    planner = QueryPlanner(
        _settings(query_planner_cache_ttl_seconds=60),
        llm,
    )

    first = await planner.plan("缓存这个规划")
    second = await planner.plan("缓存这个规划")

    assert first.source == "llm"
    assert second.source == "cache"
    llm.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_variants_keep_original_and_deduplicate_chunks() -> None:
    async def retrieve(query, _top_k, _filters):
        if query == "原始":
            return [
                {"chunk_id": "shared", "score": 0.4},
                {"chunk_id": "original-only", "score": 0.8},
            ]
        return [
            {"chunk_id": "shared", "score": 0.9},
            {"chunk_id": "rewrite-only", "score": 0.7},
        ]

    results = await _retrieve_query_variants(
        SimpleNamespace(retrieve=retrieve),
        ["原始", "改写", "改写"],
        10,
        {},
    )

    assert [item["chunk_id"] for item in results] == [
        "shared",
        "original-only",
        "rewrite-only",
    ]
    assert results[0]["matched_queries"] == ["原始", "改写"]


@pytest.mark.asyncio
async def test_search_api_exposes_automatic_plan_and_explicit_override(client) -> None:
    knowledge_bases = await client.get("/v1/knowledge-bases")
    knowledge_base_id = knowledge_bases.json()["items"][0]["id"]

    automatic = await client.post(
        "/v1/search",
        json={
            "query": "网络安全法包括哪些条款？",
            "knowledge_base_id": knowledge_base_id,
            "enable_vector": False,
            "enable_keyword": True,
            "enable_rerank": False,
        },
    )
    assert automatic.status_code == 200
    automatic_body = automatic.json()
    assert automatic_body["query_plan"]["intent"] == "enumeration"
    assert automatic_body["max_chunks_per_document"] == 10
    assert automatic_body["coverage_status"] == "partial"

    explicit = await client.post(
        "/v1/search",
        json={
            "query": "网络安全法包括哪些条款？",
            "knowledge_base_id": knowledge_base_id,
            "retrieval_mode": "focused",
            "max_chunks_per_document": 2,
            "enable_vector": False,
            "enable_keyword": True,
            "enable_rerank": False,
        },
    )
    assert explicit.status_code == 200
    explicit_body = explicit.json()
    assert explicit_body["query_plan"]["source"] == "explicit"
    assert explicit_body["max_chunks_per_document"] == 2


@pytest.mark.asyncio
async def test_enumeration_search_expands_adjacent_chunks_within_scope() -> None:
    anchor = {
        "chunk_id": "chunk-2",
        "document_id": "doc-1",
        "chunk_index": 2,
        "text": "第二条",
        "score": 0.9,
    }
    neighbor = SimpleNamespace(
        id="chunk-3",
        document_id="doc-1",
        chunk_index=3,
        text="第三条",
        page_no=2,
        metadata_json={},
    )
    document = SimpleNamespace(
        source_uri="upload://law.pdf",
        original_filename="law.pdf",
        title="法律",
    )
    chunk_store = SimpleNamespace(
        neighbors_for_anchors=AsyncMock(return_value=[(neighbor, document)])
    )
    engine = HybridSearch(
        document_store=SimpleNamespace(
            ready_ids=AsyncMock(return_value={"doc-1"})
        ),
        chunk_store=chunk_store,
    )
    engine._vector = SimpleNamespace(retrieve=AsyncMock(return_value=[anchor]))

    result = await engine.search(
        "列出全部条款",
        knowledge_base_id="kb-1",
        enable_keyword=False,
        enable_rerank=False,
        enable_abstention=False,
        max_chunks_per_document=10,
        adjacent_chunk_radius=2,
    )

    assert [item["chunk_id"] for item in result["results"]] == [
        "chunk-2",
        "chunk-3",
    ]
    assert result["results"][1]["expanded"] is True
    chunk_store.neighbors_for_anchors.assert_awaited_once_with(
        [("doc-1", 2)],
        knowledge_base_id="kb-1",
        radius=2,
    )
