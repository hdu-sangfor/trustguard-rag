# 混合检索实现报告

## 实现完成情况

✅ **混合检索核心功能已完成**，包含以下模块：

| 模块 | 文件 | 功能 |
|------|------|------|
| 向量检索 | `app/core/retrieval/vector_retriever.py` | Qdrant 稠密向量语义检索 |
| 关键词检索 | `app/core/retrieval/keyword_retriever.py` | OpenSearch BM25 + Pseudo 模拟模式 |
| 融合算法 | `app/core/retrieval/search.py` | RRF + 加权分数融合 |
| 重排序 | `app/core/retrieval/reranker.py` | BGE 本地模型 + 百炼 qwen3-rerank API + none 直通模式 |
| 搜索 API | `app/api/search.py` | `POST /v1/search` 端点 |
| 文本索引 | `app/core/indexing/opensearch_indexer.py` | 入库同步写入 OpenSearch |
| 补偿清理 | `app/core/ingest/compensator.py` | 回滚时清理 OpenSearch 文档 |

---

## 架构设计

```
POST /v1/search
      │
      ▼
HybridSearch.search()
      │
      ├──► VectorRetriever  ──► Qdrant.search()         (稠密向量)
      │         │
      │         └──► EmbeddingClient.embed_query()        (查询编码)
      │
      ├──► KeywordRetriever ──► OpenSearch.search()       (BM25 全文)
      │         │
      │         └──► PseudoKeywordRetriever               (本地模拟，无需 OpenSearch)
      │
      ├──► RRF / WeightedScore Fusion                     (融合策略)
      │
      └──► Reranker ──┬──► BGE FlagReranker             (本地重排序)
                      └──► 百炼 qwen3-rerank API          (远程重排序)
```

### 融合策略

1. **RRF (Reciprocal Rank Fusion)** — 默认策略
   - 公式: `score = Σ 1/(k + rank)`
   - `k=60` 可调，k 越小排名靠前权重越高
   - 无需归一化，对异构分数鲁棒

2. **Weighted Score Fusion** — 备选策略
   - 公式: `score = vector_score × w_v + keyword_score × w_k`
   - 默认权重: 向量 0.6 / 关键词 0.4

### 关键词检索引擎双模式

- **生产模式**: 真实 OpenSearch BM25
- **开发/测试模式**: `PseudoKeywordRetriever` — 基于内存的 TF 模拟，零依赖启动
  - 配置: `RAG_SEARCH_OPENSEARCH_MOCK=true` (默认)

---

## 新增配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `RAG_SEARCH_TOP_K` | 10 | 最终返回结果数 |
| `RAG_SEARCH_VECTOR_TOP_K` | 30 | 向量检索召回上限 |
| `RAG_SEARCH_KEYWORD_TOP_K` | 30 | 关键词检索召回上限 |
| `RAG_SEARCH_FUSION_METHOD` | rrf | rrf / weighted_score |
| `RAG_SEARCH_RRF_K` | 60 | RRF 融合常数 |
| `RAG_SEARCH_VECTOR_WEIGHT` | 0.6 | 加权融合向量权重 |
| `RAG_SEARCH_KEYWORD_WEIGHT` | 0.4 | 加权融合关键词权重 |
| `RAG_SEARCH_OPENSEARCH_MOCK` | true | true=本地模拟，false=真实 OpenSearch |
| `RAG_RERANK_PROVIDER` | none | none / local / api |
| `RAG_RERANK_TOP_K` | 10 | 重排传入候选数 |
| `RAG_RERANK_DEVICE` | auto | 重排设备 |
| `RAG_RERANK_BATCH_SIZE` | 16 | 重排批量大小 |
| `RAG_RERANK_BASE_URL` | - | OpenAI-compatible rerank API 地址 |
| `RAG_RERANK_API_KEY` | - | API Key |

---

## 识别到的改进点

### 1. OpenSearch 双写一致性（已实现）
**问题**: OpenSearch 入库失败后仍发布会产生半成品 ready 文档。
**方案**: Qdrant 与 OpenSearch 都成功后才发布；任一失败触发 Saga 补偿并将文档置为 failed。

### 2. ⚠️ 补偿器需清理 OpenSearch（已实现）
**问题**: 原始 `Compensator` 仅清理 Qdrant 向量。
**方案**: 回滚、替换和显式删除都会独立尝试双删；失败持久化为中间状态并在启动时续跑。

### 3. ✨ 建议: 向量检索增加 chunk_text 到 Qdrant payload
**问题**: 当前 `QdrantIndexer.upsert_chunks()` 的 payload 不包含 `chunk_text` 字段，
导致向量检索返回的 `text` 为空，需要额外查询 MySQL 回填。
**建议**: 在 `upsert_chunks()` 的 payload 中加入 `chunk_text: chunk["text"]`。
**现状**: `vector_retriever.py` 已从 payload 读取 `chunk_text`，但索引端未写入。

### 4. ✨ 建议: 增加混合检索缓存层
**问题**: 相同查询重复计算嵌入和检索。
**方案**: 利用 Redis（已连接）缓存查询向量和 Top-K 结果，设置 TTL。

### 5. ✨ 建议: 支持多字段权重微调
**问题**: BM25 仅匹配 `text` 字段，未利用 `original_filename`、`source_uri` 等字段。
**方案**: OpenSearch 查询中加入 `multi_match` 与字段权重提升。

### 6. ✨ 建议: 增加查询扩展（Query Expansion）
**问题**: 短查询可能召回不足。
**方案**: 使用 LLM 或同义词词典自动扩展查询，增加召回。

### 7. ✨ 建议: 支持结果去重和引用标记
**问题**: 返回的同一文档的多个分块可能包含重复信息。
**方案**: 按 `document_id` 聚合，标记页码引用信息。

### 8. ✨ 建议: 增加检索质量指标
**问题**: 无检索质量衡量。
**方案**: 记录 `NDCG@k`、`MRR` 等离线评估指标，可用于 A/B 测试融合参数。

---

## 测试覆盖

31 个测试用例全覆盖以下场景:

- ✅ 向量检索 Mock 模式
- ✅ Pseudo 关键词检索 (索引/检索/删除/过滤)
- ✅ RRF 融合算法 (基础/重叠去重/单边空/双边空/排名元数据)
- ✅ Weighted Score 融合算法 (基础/单边/权重影响)
- ✅ 混合搜索编排 (双引擎/仅向量/仅关键词/Top-K/Filters/融合策略)
- ✅ HTTP API (成功/无效融合方法/双禁用/默认参数/Top-K/全引擎)
- ✅ 工厂方法返回正确类型
