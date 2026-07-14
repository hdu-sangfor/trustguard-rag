# 混合检索 (Hybrid Search)

TrustGuard RAG 平台的混合检索引擎，支持**稠密向量检索 + BM25 关键词检索 + 融合重排**。

## 快速开始

### 1. 配置环境变量

```bash
# .env 或环境变量
RAG_SEARCH_TOP_K=10
RAG_SEARCH_VECTOR_TOP_K=30
RAG_SEARCH_KEYWORD_TOP_K=30
RAG_SEARCH_FUSION_METHOD=rrf          # rrf | weighted_score
RAG_SEARCH_RRF_K=60
RAG_SEARCH_OPENSEARCH_MOCK=true       # true=本地模拟, false=真实OpenSearch
RAG_RERANK_PROVIDER=none              # none | bge
```

### 2. 搜索 API

```bash
# 基本搜索
curl -X POST http://localhost:18200/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "SQL注入攻击防御方法",
    "top_k": 10
  }'

# 仅向量检索
curl -X POST http://localhost:18200/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "网络安全威胁检测",
    "enable_keyword": false
  }'

# 仅关键词检索
curl -X POST http://localhost:18200/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "XSS跨站脚本攻击",
    "enable_vector": false
  }'

# 加权分数融合 + 元数据过滤
curl -X POST http://localhost:18200/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "密码学安全协议",
    "fusion_method": "weighted_score",
    "vector_weight": 0.7,
    "keyword_weight": 0.3,
    "filters": {"source_uri": "file:///security-guide.pdf"}
  }'
```

### 3. 响应格式

```json
{
  "query": "SQL注入攻击防御方法",
  "results": [
    {
      "chunk_id": "uuid",
      "text": "分块文本内容...",
      "score": 0.85,
      "vector_score": 0.92,
      "keyword_score": 7.5,
      "rerank_score": null,
      "source": {
        "document_id": "doc-uuid",
        "source_uri": "file:///security-guide.pdf",
        "original_filename": "security-guide.pdf",
        "chunk_index": 3,
        "page_no": 12
      },
      "metadata": {}
    }
  ],
  "total": 10,
  "fusion_method": "rrf",
  "retrieval_time_ms": 45.2,
  "components": {
    "vector": 8,
    "keyword": 12
  }
}
```

## 架构

```
                  POST /v1/search
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
     VectorRetriever      KeywordRetriever
     (Qdrant 向量)         (OpenSearch BM25)
              │                   │
              └─────────┬─────────┘
                        ▼
              融合 (RRF / Weighted)
                        │
                        ▼
                   Reranker
                 (BGE / none)
                        │
                        ▼
                   Top-K 结果
```

## 融合策略

### RRF (Reciprocal Rank Fusion) — 默认

```python
score = Σ 1 / (k + rank)
# k=60, k 越小则排名靠前权重越高
```

- **优点**: 无需归一化，对异构分数鲁棒
- **适用**: 向量和 BM25 分数尺度差异大

### Weighted Score

```python
score = vector_score × w_v + keyword_score × w_k
# 默认 w_v=0.6, w_k=0.4
```

- **优点**: 可精确控制检索引擎权重
- **适用**: 分数归一化后，需要明确控制偏向

## 重排序

### BGE Reranker (需要 FlagEmbedding)

```bash
pip install FlagEmbedding
```

```bash
RAG_RERANK_PROVIDER=bge
RAG_RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

### None (直通)

```bash
RAG_RERANK_PROVIDER=none
```

## 开发/测试

### Pseudo 关键词检索（无需 OpenSearch）

默认 `RAG_SEARCH_OPENSEARCH_MOCK=true` 时使用内存模拟 BM25，基于查询词命中率计算分数，零外部依赖即可测试混合检索完整流程。

```bash
# 运行测试
python -m pytest tests/test_hybrid_search.py -v
```

### 真实 OpenSearch 环境

```bash
RAG_SEARCH_OPENSEARCH_MOCK=false
RAG_OPENSEARCH_HOST=localhost
RAG_OPENSEARCH_PORT=9200
```

## 入库时自动索引 OpenSearch

文档入库流水线会自动将分块文本写入 OpenSearch（失败不影响主流程）：

```
ingest pipeline:
  validate → extract → dedup → conflict_check
  → commit_artifacts → chunk → embed
  → qdrant_index → opensearch_index → publish
```

回滚/冲突删除时会自动清理 OpenSearch 中的文档。

## 配置参考

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `RAG_SEARCH_TOP_K` | 10 | 最终返回结果数 |
| `RAG_SEARCH_VECTOR_TOP_K` | 30 | 向量检索召回上限 |
| `RAG_SEARCH_KEYWORD_TOP_K` | 30 | 关键词检索召回上限 |
| `RAG_SEARCH_FUSION_METHOD` | rrf | rrf 或 weighted_score |
| `RAG_SEARCH_RRF_K` | 60 | RRF 融合常数 |
| `RAG_SEARCH_VECTOR_WEIGHT` | 0.6 | 加权融合向量权重 |
| `RAG_SEARCH_KEYWORD_WEIGHT` | 0.4 | 加权融合关键词权重 |
| `RAG_SEARCH_OPENSEARCH_MOCK` | true | 本地模拟 OpenSearch |
| `RAG_RERANK_PROVIDER` | bge | bge 或 none |
| `RAG_RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | 重排序模型 |
| `RAG_RERANK_TOP_K` | 10 | 重排候选数 |
| `RAG_RERANK_DEVICE` | auto | 设备选择 |
