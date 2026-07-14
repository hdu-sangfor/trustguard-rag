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
RAG_RERANK_PROVIDER=none              # none | local | api
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
  },
  "degraded_components": []
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
              (BGE / 百炼 / none)
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
RAG_RERANK_PROVIDER=local
RAG_RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

### 百炼 API（推荐 qwen3-rerank）

```bash
RAG_RERANK_PROVIDER=api
RAG_RERANK_MODEL=qwen3-rerank
RAG_RERANK_BASE_URL=https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-api/v1
RAG_RERANK_API_KEY=YOUR_BAILIAN_API_KEY
# 可选：自定义排序任务，建议使用英文
RAG_RERANK_INSTRUCTION=Given a web search query, retrieve relevant passages that answer the query.
```

也可以使用百炼标准环境变量 `DASHSCOPE_API_KEY` 代替
`RAG_RERANK_API_KEY`。调用失败时会记录 warning，并回退到融合后的原始排序。
兼容旧习惯：`bge` 会归一化为 `local`，`bailian`、`dashscope`、
`openai_compatible` 和 `remote` 会归一化为 `api`。

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

文档入库流水线会自动将分块文本写入 Qdrant 和 OpenSearch；两侧都成功后才发布：

```
ingest pipeline:
  validate → extract → dedup → conflict_check
  → commit_artifacts → chunk → embed
  → qdrant_index → opensearch_index → publish
```

回滚/冲突删除时会自动清理 OpenSearch 中的文档。
任一索引写入失败都会触发 Saga 补偿，文档进入 `failed` 而不是 `ready`。删除和替换分别
使用 `deleting`、`superseeding` 中间状态；Qdrant/OpenSearch 双删会独立执行并支持幂等
重试。检索结果还会根据 MySQL 中的 `ready` 状态做权威过滤，防止清理期间的孤儿索引泄漏。
应用启动时还会把 MySQL 中所有 ready 文档的 active 分块幂等回填到 OpenSearch，
用于处理后接入 OpenSearch、索引卷重建以及历史写入失败等情况。搜索时若发现业务索引
不存在，也会自动创建并触发回填。

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
| `RAG_OPENSEARCH_BACKFILL_ON_STARTUP` | true | 启动时回填历史 ready 文档 |
| `RAG_CLEANUP_RESUME_ON_STARTUP` | true | 启动时续跑 deleting/superseeding/failed 清理 |
| `RAG_RERANK_PROVIDER` | none | none、local 或 api |
| `RAG_RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | 重排序模型 |
| `RAG_RERANK_TOP_K` | 10 | 重排候选数 |
| `RAG_RERANK_DEVICE` | auto | 设备选择 |
| `RAG_RERANK_BASE_URL` | - | OpenAI-compatible rerank API 地址 |
| `RAG_RERANK_API_KEY` | - | API Key，百炼也可使用 DASHSCOPE_API_KEY |
| `RAG_RERANK_API_TIMEOUT_SECONDS` | 60 | API 请求超时时间 |
| `RAG_RERANK_INSTRUCTION` | - | qwen3-rerank 排序任务指令 |
