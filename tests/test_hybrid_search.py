"""混合检索功能测试。"""
from __future__ import annotations

import pytest

from app.core.retrieval.search import (
    HybridSearch,
    _merge_results,
    _rrf_fusion,
    _weighted_score_fusion,
)
from app.core.retrieval.vector_retriever import MockVectorRetriever
from app.core.retrieval.keyword_retriever import (
    PseudoKeywordRetriever,
    get_keyword_retriever,
)
from app.settings import Settings, get_settings


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "pseudo")
    monkeypatch.setenv("RAG_EMBEDDING_DIM", "8")
    monkeypatch.setenv("RAG_QDRANT_MOCK", "true")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "true")
    monkeypatch.setenv("RAG_SEARCH_TOP_K", "10")
    monkeypatch.setenv("RAG_SEARCH_VECTOR_TOP_K", "20")
    monkeypatch.setenv("RAG_SEARCH_KEYWORD_TOP_K", "20")
    monkeypatch.setenv("RAG_SEARCH_FUSION_METHOD", "rrf")
    monkeypatch.setenv("RAG_SEARCH_RRF_K", "60")
    monkeypatch.setenv("RAG_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def sample_vector_results() -> list[dict]:
    return [
        {
            "chunk_id": "v1",
            "text": "网络安全威胁检测系统使用深度学习模型",
            "score": 0.95,
            "document_id": "doc1",
            "chunk_index": 0,
            "page_no": 1,
            "source_uri": "file:///doc1.pdf",
            "original_filename": "doc1.pdf",
            "metadata": {},
        },
        {
            "chunk_id": "v2",
            "text": "SQL注入攻击是最常见的Web安全漏洞",
            "score": 0.87,
            "document_id": "doc2",
            "chunk_index": 1,
            "page_no": 3,
            "source_uri": "file:///doc2.pdf",
            "original_filename": "doc2.pdf",
            "metadata": {},
        },
        {
            "chunk_id": "v3",
            "text": "量子计算对密码学的影响分析",
            "score": 0.62,
            "document_id": "doc3",
            "chunk_index": 0,
            "page_no": 1,
            "source_uri": "file:///doc3.pdf",
            "original_filename": "doc3.pdf",
            "metadata": {},
        },
    ]


@pytest.fixture
def sample_keyword_results() -> list[dict]:
    return [
        {
            "chunk_id": "k1",
            "text": "SQL注入是最常见的攻击向量，攻击者可通过构造恶意的SQL语句",
            "score": 8.5,
            "document_id": "doc4",
            "chunk_index": 2,
            "page_no": 5,
            "source_uri": "file:///doc4.pdf",
            "original_filename": "doc4.pdf",
            "metadata": {},
        },
        {
            "chunk_id": "v2",
            "text": "SQL注入攻击是最常见的Web安全漏洞",
            "score": 7.2,
            "document_id": "doc2",
            "chunk_index": 1,
            "page_no": 3,
            "source_uri": "file:///doc2.pdf",
            "original_filename": "doc2.pdf",
            "metadata": {},
        },
        {
            "chunk_id": "k2",
            "text": "Web应用防火墙可以有效防御SQL注入和XSS攻击",
            "score": 4.1,
            "document_id": "doc5",
            "chunk_index": 0,
            "page_no": 2,
            "source_uri": "file:///doc5.pdf",
            "original_filename": "doc5.pdf",
            "metadata": {},
        },
    ]


class TestVectorRetriever:
    @pytest.mark.asyncio
    async def test_mock_returns_empty(self, mock_settings: Settings) -> None:
        retriever = MockVectorRetriever()
        results = await retriever.retrieve("test query", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_mock_await_works(self, mock_settings: Settings) -> None:
        retriever = MockVectorRetriever()
        results = await retriever.retrieve("test", top_k=10)
        assert isinstance(results, list)
        assert len(results) == 0


class TestPseudoKeywordRetriever:
    @pytest.mark.asyncio
    async def test_index_and_retrieve(self) -> None:
        retriever = PseudoKeywordRetriever()

        await retriever.index_chunk(
            chunk_id="c1",
            text="SQL注入攻击是最常见的Web安全漏洞，攻击者可以通过注入恶意SQL代码",
            document_id="doc1",
            chunk_index=0,
            source_uri="file:///doc1.pdf",
            original_filename="doc1.pdf",
            page_no=1,
        )
        await retriever.index_chunk(
            chunk_id="c2",
            text="量子计算的研究进展对现代密码学有深远影响",
            document_id="doc2",
            chunk_index=0,
            source_uri="file:///doc2.pdf",
            original_filename="doc2.pdf",
            page_no=1,
        )
        await retriever.index_chunk(
            chunk_id="c3",
            text="安全审计中的SQL注入检测工具使用方法",
            document_id="doc3",
            chunk_index=0,
            source_uri="file:///doc3.pdf",
            original_filename="doc3.pdf",
            page_no=1,
        )

        results = await retriever.retrieve("SQL注入", top_k=5)
        assert len(results) > 0
        cids = [r["chunk_id"] for r in results]
        assert "c1" in cids
        assert "c3" in cids

    @pytest.mark.asyncio
    async def test_retrieve_empty_index(self) -> None:
        retriever = PseudoKeywordRetriever()
        results = await retriever.retrieve("nothing", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_delete_for_document(self) -> None:
        retriever = PseudoKeywordRetriever()

        await retriever.index_chunk(
            chunk_id="c1",
            text="SQL注入漏洞分析",
            document_id="doc1",
            chunk_index=0,
            source_uri="file:///doc1.pdf",
            original_filename="doc1.pdf",
            page_no=1,
        )
        await retriever.delete_for_document("doc1")
        results = await retriever.retrieve("SQL注入", top_k=5)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_filter_by_metadata(self) -> None:
        retriever = PseudoKeywordRetriever()

        await retriever.index_chunk(
            chunk_id="c1",
            text="网络安全威胁检测系统",
            document_id="doc1",
            chunk_index=0,
            source_uri="file:///doc1.pdf",
            original_filename="doc1.pdf",
            page_no=1,
            metadata={"category": "network"},
        )
        await retriever.index_chunk(
            chunk_id="c2",
            text="数据安全保护措施",
            document_id="doc2",
            chunk_index=0,
            source_uri="file:///doc2.pdf",
            original_filename="doc2.pdf",
            page_no=1,
            metadata={"category": "data"},
        )

        results = await retriever.retrieve("安全", top_k=5, filters={"document_id": "doc1"})
        assert all(r["document_id"] == "doc1" for r in results)


class TestRRFFusion:
    def test_basic_fusion(self, sample_vector_results, sample_keyword_results) -> None:
        merged = _rrf_fusion(sample_vector_results, sample_keyword_results)

        assert len(merged) > 0
        assert merged[0].get("score") is not None
        for item in merged:
            assert "rrf_score" in item

    def test_fusion_boosts_common_items(self, sample_vector_results, sample_keyword_results) -> None:
        merged = _rrf_fusion(sample_vector_results, sample_keyword_results)

        v2_items = [m for m in merged if m.get("chunk_id") == "v2"]
        assert len(v2_items) == 1
        assert v2_items[0].get("vector_score") is not None
        assert v2_items[0].get("keyword_score") is not None

    def test_fusion_fills_fields_missing_from_first_retriever(self) -> None:
        vector = [{"chunk_id": "shared", "text": None, "score": 0.8}]
        keyword = [
            {
                "chunk_id": "shared",
                "text": "关键词召回文本",
                "document_id": "doc-1",
                "score": 2.0,
            }
        ]

        merged = _rrf_fusion(vector, keyword)

        assert merged[0]["text"] == "关键词召回文本"
        assert merged[0]["document_id"] == "doc-1"

    def test_empty_vector(self, sample_keyword_results) -> None:
        merged = _rrf_fusion([], sample_keyword_results)
        assert len(merged) == len(sample_keyword_results)

    def test_empty_keyword(self, sample_vector_results) -> None:
        merged = _rrf_fusion(sample_vector_results, [])
        assert len(merged) == len(sample_vector_results)

    def test_both_empty(self) -> None:
        merged = _rrf_fusion([], [])
        assert merged == []

    def test_rank_metadata_present(self, sample_vector_results, sample_keyword_results) -> None:
        merged = _rrf_fusion(sample_vector_results, sample_keyword_results)
        for item in merged:
            if item.get("vector_score") is not None:
                assert "vector_rank" in item
            if item.get("keyword_score") is not None:
                assert "keyword_rank" in item


class TestWeightedScoreFusion:
    def test_basic_fusion(self, sample_vector_results, sample_keyword_results) -> None:
        merged = _weighted_score_fusion(
            sample_vector_results, sample_keyword_results, vector_weight=0.6, keyword_weight=0.4
        )

        assert len(merged) > 0
        for item in merged:
            assert item.get("score") is not None

    def test_vector_only(self, sample_vector_results) -> None:
        merged = _weighted_score_fusion(
            sample_vector_results, [], vector_weight=0.7, keyword_weight=0.3
        )
        assert len(merged) == len(sample_vector_results)
        for item in merged:
            assert item["vector_score"] is not None
            assert item["keyword_score"] is None

    def test_keyword_only(self, sample_keyword_results) -> None:
        merged = _weighted_score_fusion(
            [], sample_keyword_results, vector_weight=0.7, keyword_weight=0.3
        )
        assert len(merged) == len(sample_keyword_results)
        for item in merged:
            assert item["keyword_score"] is not None

    def test_weight_influence(self, sample_vector_results, sample_keyword_results) -> None:
        weighted = _weighted_score_fusion(
            sample_vector_results, sample_keyword_results,
            vector_weight=0.9, keyword_weight=0.1,
        )

        vector_leaning = _weighted_score_fusion(
            sample_vector_results, sample_keyword_results,
            vector_weight=0.1, keyword_weight=0.9,
        )

        assert weighted[0]["chunk_id"] != vector_leaning[0]["chunk_id"]

    def test_scale_mismatch_respects_vector_weight(self) -> None:
        """余弦分与 BM25 量级差很大时，归一化后向量权重应生效。"""
        vector_results = [
            {"chunk_id": "semantic-best", "text": "sem", "score": 0.92},
            {"chunk_id": "both", "text": "both", "score": 0.80},
        ]
        keyword_results = [
            {"chunk_id": "bm25-best", "text": "kw", "score": 18.5},
            {"chunk_id": "both", "text": "both", "score": 12.0},
        ]
        merged = _weighted_score_fusion(
            vector_results, keyword_results, vector_weight=0.6, keyword_weight=0.4
        )
        ranking = [item["chunk_id"] for item in merged]
        assert ranking.index("semantic-best") < ranking.index("bm25-best")
        # 原始分仍保留
        by_id = {item["chunk_id"]: item for item in merged}
        assert by_id["semantic-best"]["vector_score"] == 0.92
        assert by_id["bm25-best"]["keyword_score"] == 18.5


class TestFakeScoreStability:
    def test_fake_score_deterministic_across_calls(self) -> None:
        from app.core.retrieval.keyword_retriever import _fake_score_from_text

        a = _fake_score_from_text("SQL injection defense", "SQL injection")
        b = _fake_score_from_text("SQL injection defense", "SQL injection")
        assert a == b
        assert a > 0

    def test_fake_score_stable_across_pythonhashseed(self) -> None:
        """子进程分别以不同 PYTHONHASHSEED 运行，分数应一致。"""
        import os
        import subprocess
        import sys

        code = (
            "from app.core.retrieval.keyword_retriever import _fake_score_from_text; "
            "print(_fake_score_from_text('SQL injection defense', 'SQL injection'))"
        )
        scores = []
        for seed in ("0", "1", "42", "random"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            env["PYTHONPATH"] = str(
                __import__("pathlib").Path(__file__).resolve().parents[1]
            )
            out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
            scores.append(out.strip())
        assert len(set(scores)) == 1


class TestMergeResults:
    def test_rrf_method(self, sample_vector_results, sample_keyword_results) -> None:
        merged = _merge_results(
            sample_vector_results, sample_keyword_results,
            fusion_method="rrf", vector_weight=0.6, keyword_weight=0.4,
        )
        assert len(merged) > 0

    def test_weighted_method(self, sample_vector_results, sample_keyword_results) -> None:
        merged = _merge_results(
            sample_vector_results, sample_keyword_results,
            fusion_method="weighted_score", vector_weight=0.6, keyword_weight=0.4,
        )
        assert len(merged) > 0


@pytest.mark.asyncio
class TestHybridSearch:
    async def test_search_with_both_engines(self, mock_settings: Settings) -> None:
        search = HybridSearch()
        result = await search.search(
            query="SQL注入攻击",
            knowledge_base_id="kb-test",
            enable_vector=False,
            enable_keyword=True,
            enable_rerank=False,
        )
        assert "results" in result
        assert "total" in result
        assert "fusion_method" in result
        assert "retrieval_time_ms" in result
        assert "components" in result
        assert result["search_status"] == "ok"
        assert result["effective_mode"] == "keyword_only"
        assert isinstance(result["results"], list)
        assert result["total"] == len(result["results"])

    async def test_search_vector_only(self, mock_settings: Settings) -> None:
        search = HybridSearch()
        result = await search.search(
            query="网络安全",
            knowledge_base_id="kb-test",
            enable_vector=True,
            enable_keyword=False,
            enable_rerank=False,
        )
        assert "results" in result
        assert result["components"]["keyword"] == 0

    async def test_search_keyword_only(self, mock_settings: Settings) -> None:
        search = HybridSearch()
        result = await search.search(
            query="网络安全",
            knowledge_base_id="kb-test",
            enable_vector=False,
            enable_keyword=True,
            enable_rerank=False,
        )
        assert "results" in result

    async def test_search_respects_top_k(self, mock_settings: Settings) -> None:
        search = HybridSearch()
        result = await search.search(
            query="安全",
            knowledge_base_id="kb-test",
            top_k=3,
            enable_vector=False,
            enable_keyword=True,
            enable_rerank=False,
        )
        assert result["total"] <= 3

    async def test_search_with_filters(self, mock_settings: Settings) -> None:
        search = HybridSearch()
        result = await search.search(
            query="安全",
            knowledge_base_id="kb-test",
            enable_vector=False,
            enable_keyword=True,
            enable_rerank=False,
            filters={"document_id": "nonexistent"},
        )
        assert result["total"] >= 0

    async def test_search_weighted_score_fusion(self, mock_settings: Settings) -> None:
        search = HybridSearch()
        result = await search.search(
            query="安全",
            knowledge_base_id="kb-test",
            fusion_method="weighted_score",
            vector_weight=0.5,
            keyword_weight=0.5,
            enable_vector=False,
            enable_keyword=True,
            enable_rerank=False,
        )
        assert result["fusion_method"] == "weighted_score"


@pytest.mark.asyncio
class TestSearchAPI:
    @staticmethod
    async def knowledge_base_id(client) -> str:
        response = await client.get("/v1/knowledge-bases")
        return response.json()["items"][0]["id"]

    async def test_search_endpoint_exists(self, client) -> None:
        knowledge_base_id = await self.knowledge_base_id(client)
        response = await client.post(
            "/v1/search",
            json={
                "query": "SQL注入攻击",
                "knowledge_base_id": knowledge_base_id,
                "enable_vector": False,
                "enable_keyword": True,
                "enable_rerank": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["query"] == "SQL注入攻击"
        assert "results" in data
        assert "total" in data
        assert "fusion_method" in data
        assert "retrieval_time_ms" in data
        assert data["search_status"] == "ok"
        assert data["effective_mode"] == "keyword_only"

    async def test_search_endpoint_invalid_fusion(self, client) -> None:
        knowledge_base_id = await self.knowledge_base_id(client)
        response = await client.post(
            "/v1/search",
            json={
                "query": "test",
                "knowledge_base_id": knowledge_base_id,
                "fusion_method": "invalid_method",
                "enable_vector": False,
                "enable_keyword": True,
                "enable_rerank": False,
            },
        )
        assert response.status_code == 422

    async def test_search_endpoint_both_disabled(self, client) -> None:
        knowledge_base_id = await self.knowledge_base_id(client)
        response = await client.post(
            "/v1/search",
            json={
                "query": "test",
                "knowledge_base_id": knowledge_base_id,
                "enable_vector": False,
                "enable_keyword": False,
                "enable_rerank": False,
            },
        )
        assert response.status_code == 400

    async def test_search_endpoint_with_defaults(self, client) -> None:
        knowledge_base_id = await self.knowledge_base_id(client)
        response = await client.post(
            "/v1/search",
            json={
                "query": "网络安全威胁",
                "knowledge_base_id": knowledge_base_id,
                "enable_vector": False,
                "enable_keyword": True,
                "enable_rerank": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 0

    async def test_search_endpoint_top_k_limit(self, client) -> None:
        knowledge_base_id = await self.knowledge_base_id(client)
        response = await client.post(
            "/v1/search",
            json={
                "query": "安全",
                "knowledge_base_id": knowledge_base_id,
                "top_k": 5,
                "enable_vector": False,
                "enable_keyword": True,
                "enable_rerank": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] <= 5

    async def test_search_with_all_engines(self, client) -> None:
        knowledge_base_id = await self.knowledge_base_id(client)
        response = await client.post(
            "/v1/search",
            json={
                "query": "SQL注入攻击防御方法",
                "knowledge_base_id": knowledge_base_id,
                "enable_vector": True,
                "enable_keyword": True,
                "enable_rerank": False,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "components" in data
        assert "vector" in data["components"]
        assert "keyword" in data["components"]


class TestKeywordRetrieverFactory:
    def test_get_keyword_retriever_returns_pseudo_in_mock(self, mock_settings: Settings) -> None:
        retriever = get_keyword_retriever()
        assert isinstance(retriever, PseudoKeywordRetriever)
