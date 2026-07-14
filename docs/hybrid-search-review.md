# 混合检索项目审查与改进建议

> 修复状态（2026-07-14）：本文最初识别的“OpenSearch 失败仍发布”和“双写/双删缺少
> 恢复状态”已经修复。现在双索引成功后才发布；删除/替换使用持久化中间状态、启动续跑，
> 检索结果由 MySQL ready 状态做最终过滤。其余性能和质量建议仍按本文路线推进。

> 审查日期：2026-07-14  
> 审查范围：Qdrant 向量召回、OpenSearch BM25、融合、重排、入库一致性、部署、测试与可观测性

## 1. 结论

保留 **Qdrant + OpenSearch** 的技术路线是合理的：Qdrant 负责高性能语义向量召回，OpenSearch 负责 BM25、精确词项、复杂过滤和全文检索，两者通过 RRF 或归一化加权融合，再使用 Cross-Encoder 重排。

但当前实现仍属于功能原型，距离高质量生产方案主要存在以下差距：

1. Docker 尚未部署真实 OpenSearch。
2. 默认 BGE Reranker 没有安装，重排会静默降级。
3. OpenSearch 写入失败后文档仍会被标记为 `READY`。
4. 检索异常会被吞掉，服务故障与“没有结果”无法区分。
5. Qdrant 与 OpenSearch 的字段和过滤契约不一致。
6. 中文分块和 OpenSearch Analyzer 不适合高质量安全语料检索。
7. Rerank 候选数量过少，无法有效改善召回结果。
8. 缺少真实双引擎集成测试和离线检索评测集。

现有 65 项自动化测试可以通过，但混合检索测试主要运行在 mock/pseudo 模式，尚不能证明真实 Qdrant + OpenSearch 链路可用。

## 2. 问题优先级

| 优先级 | 问题 | 主要影响 |
| --- | --- | --- |
| P0 | Docker 没有 OpenSearch | 生产环境无法使用真实 BM25 |
| P0 | BGE Reranker 未安装 | 开启重排后仍返回原始顺序 |
| P0 | OpenSearch 失败仍发布文档 | 双引擎数据不一致 |
| P0 | 检索异常被静默吞掉 | 故障被伪装成空结果 |
| P1 | 字段与过滤契约不一致 | 同一过滤条件只能命中一个引擎 |
| P1 | 中文分块与分词不足 | 限制关键词和语义召回上限 |
| P1 | Rerank 默认只处理 10 条 | 无法挽救融合排名靠后的相关结果 |
| P1 | Bulk 部分失败未检查 | OpenSearch 可能缺少部分 Chunk |
| P1 | 缺少真实集成测试 | 无法验证双写、双删和故障恢复 |
| P2 | 缺少多样性与上下文扩展 | Top K 容易被同一文档的相邻 Chunk 占满 |
| P2 | 缺少分阶段耗时与降级信息 | 无法定位性能和质量问题 |
| P2 | 缺少离线评测集 | 无法科学调整参数与模型 |

## 3. 当前架构问题

### 3.1 OpenSearch 未实际部署

当前 `docker-compose.yml` 没有 OpenSearch 服务，也没有为 `rag-service` 设置：

```env
RAG_OPENSEARCH_HOST=opensearch
RAG_OPENSEARCH_PORT=9200
RAG_SEARCH_OPENSEARCH_MOCK=false
```

同时，Compose 使用 `RAG_MODE=ingest`，就绪检查不会把 OpenSearch 当作必要依赖。

建议：

- 增加 OpenSearch 服务、健康检查、持久卷和资源限制。
- 将 OpenSearch 加入 `depends_on` 和 readiness 必要依赖。
- 固定经过测试的 OpenSearch 镜像及 Python 客户端版本。
- 增加认证、TLS、快照和恢复策略。
- 为本地开发和 CI 提供真实双引擎 profile。

### 3.2 Reranker 实际未生效

配置默认使用：

```env
RAG_RERANK_PROVIDER=bge
RAG_RERANK_MODEL=BAAI/bge-reranker-v2-m3
```

但项目依赖中没有 `FlagEmbedding`。模型加载失败后，代码会记录 warning 并返回未重排的结果。

建议：

- 增加独立的 `rerank` optional dependency。
- 应用启动时加载并预热模型，失败时 readiness 返回异常。
- API 返回 `rerank_applied` 和降级原因。
- 根据 CPU/GPU 自动决定是否启用 FP16。
- 增加推理并发限制、超时、批处理和长度截断。
- 将 Embedding 与 Reranker 从 Web 进程拆分为独立推理服务或专用 Worker。

### 3.3 双引擎一致性不足

当前入库顺序大致是：

```text
生成 Chunk
  → 写入 Qdrant
  → 写入 MySQL Chunk
  → 写入 OpenSearch
  → 标记 READY
```

OpenSearch 写入失败只会记录 warning，随后仍然发布文档。此外，Qdrant 在文档进入 `READY` 前已经可以检索，而查询没有发布状态过滤。

建议引入独立索引状态：

```text
document_status
qdrant_index_status
opensearch_index_status
index_version
indexed_at
last_index_error
```

严格发布流程：

```text
数据库创建文档
  → 生成并保存 Chunk
  → Qdrant 幂等写入成功
  → OpenSearch 幂等写入成功
  → 校验两边 Chunk 数量、版本和校验和
  → 标记 READY
```

如果允许单引擎降级，应使用明确的 `READY_DEGRADED`，不能将不完整索引标记为正常 `READY`。

推荐使用 MySQL Outbox + RabbitMQ Worker：

```text
MySQL 事务写入文档、Chunk、Outbox Event
                    ↓
              RabbitMQ Worker
               ├─ Qdrant
               └─ OpenSearch
                    ↓
              状态确认与重试
```

当前 RabbitMQ 仅用于健康检查，入库任务仍运行在 FastAPI `BackgroundTasks` 中。服务重启时，进程内任务可能丢失。

### 3.4 删除与冲突替换缺少事务语义

两个索引系统无法参与 MySQL 事务，因此删除过程中可能出现一边成功、一边失败。

建议：

- 增加 `DELETING` 状态和删除任务记录。
- 先将文档设置为不可检索，再异步清理两个索引。
- 删除操作必须幂等并支持重试。
- 定期执行 MySQL、Qdrant、OpenSearch 一致性扫描。
- 冲突替换应先成功发布新文档，再下线旧文档，避免新文档发布失败后旧文档也已被删除。

## 4. 检索算法问题

### 4.1 检索异常被当作空结果

当前 OpenSearch 查询异常会返回空列表，混合检索编排器也会忽略两个 Retriever 抛出的异常。因此无法区分：

```text
没有相关结果
OpenSearch 超时或故障
Qdrant 超时或故障
```

建议在响应中增加：

```json
{
  "degraded": true,
  "components": {
    "vector": {"status": "ok", "count": 30, "latency_ms": 18},
    "keyword": {
      "status": "failed",
      "count": 0,
      "latency_ms": 3000,
      "error_code": "OPENSEARCH_TIMEOUT"
    }
  }
}
```

每个阶段应具有独立超时、有限重试、熔断和结构化日志。

### 4.2 过滤字段不一致

当前 Qdrant 使用 `doc_id`，OpenSearch 使用 `document_id`。任意 `filters` 会原样发送给两个引擎，导致相同过滤条件无法同时生效。

同时还存在：

- Qdrant 未写入通用 Chunk metadata。
- OpenSearch 的 `metadata` 设置为 `enabled: false`，不能搜索其子字段。
- API 却声明支持任意 metadata filter。

建议统一字段：

```text
document_id
source_uri
status
index_version
tenant_id
page_no
category
tags
```

将任意字典改成类型明确的 Filter Schema，再分别转换成 Qdrant Filter 和 OpenSearch Query DSL。

Qdrant 应在入库前为常用过滤字段建立 Payload Index。官方文档指出 Payload Index 会直接影响过滤效率和向量查询规划：

- https://qdrant.tech/documentation/manage-data/indexing/

### 4.3 Weighted Score 未归一化

当前实现直接计算：

```text
vector_score × vector_weight + bm25_score × keyword_weight
```

余弦相似度通常接近 `0～1`，BM25 可能是 `2、10、30`，两者不能直接相加。即使配置向量权重为 0.6，BM25 仍可能主导结果。

建议：

- 默认继续使用 RRF。
- 增加 Weighted RRF。
- Weighted Score 必须先执行 Min-Max、Z-Score 或分布归一化。
- 校验权重之和等于 1。
- 记录两个引擎的排名、原始分数、归一化分数和融合贡献。

OpenSearch 2.19 已支持带权 RRF：

- https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/

### 4.4 Rerank 候选数量过少

当前 `rerank_top_k` 默认是 10，最终 `top_k` 也默认是 10。这只能重新排列已经位于前 10 的结果，不能挽救排名 11～50 的相关 Chunk。

建议从以下配置开始建立评测：

```text
Qdrant recall:      50～100
OpenSearch recall:  50～100
RRF candidates:     60～100
Rerank candidates:  30～60
Final results:      10
```

最终数值必须通过真实评测集确定。

### 4.5 缺少多样性与上下文扩展

当前 Top K 可能被同一个文档的相邻 Chunk 占满。

建议在重排后增加：

- 每个文档最大结果数。
- MMR 或相似 Chunk 去重。
- 相邻 Chunk 合并。
- Parent section 扩展。
- 表格、代码块完整性保护。
- 根据回答任务动态补取上下文。

## 5. 中文与安全语料优化

### 5.1 分块策略

当前使用 `len(text) // 4` 估算 Token，对中文明显不准确；同时没有 overlap，超长段落会直接按字符硬切。

建议：

- 使用实际 Embedding 模型的 Tokenizer。
- 以 256～512 tokens 为初始实验范围。
- 增加 10%～20% overlap。
- 按标题、章节、段落、列表、代码块和表格语义切分。
- Embedding 输入加入文档标题、章节路径和页码。
- 保存原始 Chunk 用于引用，使用 contextual chunk 生成向量。
- 后续加入 parent-child retrieval。

### 5.2 OpenSearch Analyzer 与字段设计

当前只使用 `standard` analyzer，不适合中文和复杂安全标识。

建议建立多字段：

```text
text.standard
text.cjk / text.icu
text.exact
title
headings
original_filename
identifiers.keyword
code_tokens.keyword
```

字段权重可从以下方案开始评测：

```text
identifiers^10
title^4
headings^2
text^1
```

对 CVE、CWE、IP、域名、Hash、端口、文件路径、注册表路径和错误码进行实体提取和精确匹配增强。

OpenSearch 官方参考：

- CJK Analyzer：https://docs.opensearch.org/latest/analyzers/language-analyzers/cjk/
- ICU Analyzer：https://docs.opensearch.org/latest/analyzers/language-analyzers/icu/

## 6. 索引与性能优化

### 6.1 OpenSearch

当前 Bulk 写入存在两个问题：

1. 没有检查响应中的 `errors` 和失败 item。
2. 每次 Bulk 都使用 `refresh=true`。

建议：

- 使用 Bulk Helper，并按文档数量和请求大小分批。
- 检查每个失败 item，只重试失败记录。
- 使用默认刷新或 `refresh=wait_for`。
- 为写入设置超时、退避重试和失败队列。
- 使用版本化索引与读写 Alias：

```text
rag_chunks_v1
rag_chunks_read
rag_chunks_write
```

- 使用 Index Template 管理 mapping 和 settings。
- 单机开发可使用 0 副本；生产环境应规划副本、快照和容量水位。

官方参考：

- Bulk API：https://docs.opensearch.org/latest/api-reference/document-apis/bulk/
- Index Alias：https://docs.opensearch.org/latest/im-plugin/index-alias/

### 6.2 Qdrant

建议增加：

- `document_id`、`source_uri`、`status`、`index_version`、`tenant_id` Payload Index。
- `score_threshold`，减少明显无关的向量结果。
- 可配置 `hnsw_ef`，在召回率和延迟之间调优。
- Collection alias 和索引版本。
- 严格模式，阻止未索引字段过滤和过大的查询。
- 生产副本、快照和恢复演练。

## 7. 可观测性

当前只返回总耗时和两个引擎的结果数量，不足以定位性能和质量问题。

建议记录：

```text
query_embedding_ms
qdrant_ms
opensearch_ms
fusion_ms
rerank_ms
total_ms
vector_candidates
keyword_candidates
candidate_overlap
rerank_position_delta
degraded_reason
index_lag_seconds
```

同时引入 Trace ID，将搜索请求、两个引擎调用、重排和最终结果串联起来。

## 8. 测试与评测体系

### 8.1 集成测试

至少增加以下真实容器测试：

- 文档写入后能同时被 Qdrant 和 OpenSearch 召回。
- 两个引擎中的 Chunk ID、版本和数量一致。
- OpenSearch Bulk 部分失败不会发布文档。
- 任意一个引擎故障时响应明确标记降级。
- 删除后两个引擎都不能再召回文档。
- 重试不会产生重复 Chunk。
- 服务重启后未完成任务可以继续执行。
- 索引重建和 Alias 切换期间查询不中断。

### 8.2 离线评测集

准备至少 100～300 条真实安全查询，覆盖：

- 自然语言问题。
- CVE/CWE 编号。
- IP、域名、Hash。
- 产品名称和版本。
- 攻击行为与防御措施。
- 中英文混合查询。
- 否定、比较和多条件问题。

建议跟踪：

```text
Recall@20 / Recall@50
nDCG@10
MRR@10
Exact-ID Hit Rate
Rerank 改善率
无效结果率
索引不一致率
P50 / P95 / P99 延迟
```

没有评测集，就无法判断 Chunk 大小、Analyzer、RRF 参数、召回数量和 Reranker 的调整究竟是提升还是退化。

## 9. 推荐目标架构

```text
                         ┌───────────────────┐
                         │ MySQL + Outbox    │
                         │ 权威数据与索引状态 │
                         └─────────┬─────────┘
                                   │
                            RabbitMQ Worker
                         ┌─────────┴─────────┐
                         │                   │
                    Qdrant Index       OpenSearch Index
                    Dense Vector       BM25 / Exact / Filter
                         │                   │
                         └─────────┬─────────┘
                                   │
                             Weighted RRF
                                   │
                          Cross-Encoder Rerank
                                   │
                    去重 / 多样性 / 上下文扩展
                                   │
                              Final Top K
```

## 10. 推荐实施顺序

### 第一阶段：正确性

1. Docker 加入真实 OpenSearch。
2. 安装并验证 BGE Reranker。
3. 统一字段、过滤和 Chunk ID 契约。
4. OpenSearch 失败时禁止静默发布。
5. API 暴露检索降级状态。
6. 增加真实双引擎集成测试。

### 第二阶段：召回质量

1. 改造中文语义分块。
2. 增加 ICU/CJK、多字段和安全实体索引。
3. 扩大两路候选召回数量。
4. 将 Rerank 候选提高到 30～60。
5. 增加阈值、去重、文档配额和上下文扩展。

### 第三阶段：可靠性与性能

1. 使用 Outbox + RabbitMQ Worker。
2. 增加索引版本、重试和一致性巡检。
3. 使用 OpenSearch Alias 和 Qdrant Collection Alias。
4. 增加超时、熔断、并发限制和模型预热。
5. 完善分阶段指标、追踪、告警和容量规划。

### 第四阶段：评测驱动优化

1. 建立安全领域查询评测集。
2. 对 Chunk、Analyzer、RRF、候选数量和 Reranker 做消融实验。
3. 引入线上反馈、点击和引用命中数据。
4. 支持 A/B 测试和检索配置版本化。
