# TrustGuard 独立 RAG 平台落地方案

> 目标：先建设一个独立运行、独立评测、独立扩展的 RAG 平台，再通过标准 HTTP API 接入现有 trustguard-agent 项目，为网络安全智能体提供高质量、可溯源、可控权限的检索增强能力。

## 1. 背景与目标

当前 `trustguard-agent` 已经具备 Qdrant 知识库接入、经验沉淀、query 净化、软加权、层级 RAG、CVE/Blog pipeline 等基础能力，但这些能力主要散落在 orchestrator 内部，检索链路仍以 Qdrant 单路向量召回为主。

结合本地源码探索结果：

- RAGFlow 的检索链路在 `ragflow/rag/nlp/search.py` 中体现得很清楚：先构造文本匹配与向量匹配，再通过融合表达式和 `vector_similarity_weight` / `term_similarity_weight` 做综合排序，并可接 rerank 模型。
- RAGFlow 的 ingestion 侧会为 chunk 自动生成关键词、问题、metadata、tags，这对提升安全知识的可发现性很有价值。
- Dify 的 `RetrievalService` 采用 keyword、embedding、full-text 多路并发召回，hybrid 模式下去重后交给 post processor 做 weighted score 或 rerank model。
- Dify 的 dataset indexing 使用异步 worker，将文档入库与在线检索接口解耦。
- 现有项目的 `orchestrator-service/app/clients/llm_client.py` 已经有 RAG 注入点，但目前直接调用 `kb_client.py`，不适合继续堆复杂检索逻辑。

因此本方案采用：

```text
先独立建设 trustguard-rag-platform
再由 trustguard-agent 通过 RAG_SERVICE_URL 调用
```

核心目标：

1. 数据采集与清洗：支持 URL、RSS、NVD、GitHub、Markdown/PDF、本地文件；完成去噪、结构化抽取、语义分块。
2. 检索基础后端：提供独立 FastAPI 服务，负责查询接入、索引管理、召回、重排和引用返回。
3. 检索增强策略：实现查询改写、混合检索、融合排序、rerank、父子块回填，不只做简单向量匹配。
4. 工程化基建：部署向量数据库、全文索引、元数据存储、对象存储；长耗时任务通过消息队列异步解耦。
5. 项目接入：现有 TrustGuard 智能体只通过 HTTP 调用 RAG 平台，不直接感知底层索引细节。

> **本阶段范围（重要）**：当前首要任务是**尽快搭建一个可独立运行的 RAG 平台**——能采集、入库、混合检索、评测即可。与 `trustguard-agent` 的对接（第 13 节）整体**后置，本阶段不实施**；对接相关约束（chunk_id 格式兼容、tenant/workspace 映射、经验回流 API 等）留到对接阶段再处理，本文档仅保留占位说明，**不要让这些约束拖慢独立平台的开发**。

## 2. 设计原则

1. 独立性优先  
   RAG 平台必须可以脱离 `trustguard-agent` 独立启动、入库、检索和评测。

2. 检索质量优先  
   网络安全场景中，CVE、版本号、端口、路径、参数、工具名、PoC 片段等精确匹配非常关键，因此必须使用向量检索 + 字面检索 + metadata 检索的组合。

3. 可溯源优先  
   所有命中结果必须返回 `chunk_id`、`document_id`、`source_url`、`source_type`、更新时间、命中分数和 rerank 分数，方便报告生成与审计。

4. 异步解耦  
   爬虫、解析、清洗、分块、embedding、双索引写入全部走异步 job，不阻塞在线查询。

5. 多租户与安全边界  
   `tenant_id`、`project_id`、`classification`、`source_trust` 等必须作为 metadata 进入过滤条件，避免不同项目之间知识泄露。

6. 渐进迁移  
   保留现有项目中已验证的 `kb_query_purify`、`kb_retrieval_scoring`、`kb_hierarchical_rag` 思路，迁移到独立 RAG 平台内部。

## 3. 总体架构

```text
Frontend / Orchestrator / TrustGuard Agents
        |
        | HTTP
        v
+------------------------------+
| trustguard-rag-platform         |
|                              |
|  FastAPI RAG Service         |
|   - Query Analyzer           |
|   - Query Rewrite            |
|   - Vector Retriever         |
|   - Lexical Retriever        |
|   - Structured Retriever     |
|   - Fusion / Dedup           |
|   - Rerank                   |
|   - Parent Expansion         |
|   - Context Pack / Citation  |
|                              |
|  Async Workers               |
|   - Crawl                    |
|   - Parse                    |
|   - Clean                    |
|   - Chunk                    |
|   - Embed                    |
|   - Index                    |
+------------------------------+
        |
        +-- MySQL/PostgreSQL: metadata, documents, chunks, jobs
        +-- Qdrant: vector index
        +-- OpenSearch: BM25 / full-text index
        +-- MinIO: raw files and cleaned artifacts
        +-- RabbitMQ: async job queue
        +-- Redis: cache, rate limit, job heartbeat
```

建议先使用当前项目已有的 MySQL、Redis、RabbitMQ、Qdrant，新增 OpenSearch（混合检索必需）；**MinIO 在 MVP 阶段可选**——先用本地卷/目录保存 raw 与 clean 产物即可（`stores/minio_store.py` 先实现一个本地文件后端），待规模变大再切换对象存储，让独立平台尽快起步。若后续希望更适合复杂 metadata 查询，可以将 metadata DB 从 MySQL 平滑迁移到 PostgreSQL。

## 4. 推荐目录结构

```text
trustguard-rag-platform/
  app/
    main.py
    api/
      health.py
      query.py
      search.py
      ingest.py
      jobs.py
      documents.py
      chunks.py
    core/
      crawler/
        base.py
        web.py
        rss.py
        nvd.py
        github.py
        local_file.py
      parser/
        html_parser.py
        markdown_parser.py
        pdf_parser.py
        json_parser.py
      cleaner/
        boilerplate.py
        security_text.py
        dedup.py
      chunker/
        parent_child.py
        atomic_fact.py
        qa_chunk.py
      extractor/
        security_entities.py
        metadata_extractor.py
        question_generator.py
      embedding/
        provider.py
        openai_compatible.py
        local_bge.py
      indexer/
        qdrant_indexer.py
        opensearch_indexer.py
      retrieval/
        query_analyzer.py
        query_rewriter.py
        vector_retriever.py
        lexical_retriever.py
        structured_retriever.py
        fusion.py
        parent_expander.py
      rerank/
        base.py
        bge_reranker.py
        jina_reranker.py
      context_pack/
        packer.py
        citation.py
    schemas/
      api.py
      document.py
      chunk.py
      job.py
      retrieval.py
    stores/
      db.py
      document_store.py
      chunk_store.py
      job_store.py
      qdrant_store.py
      opensearch_store.py
      minio_store.py
      redis_cache.py
    workers/
      worker.py
      handlers.py
      scheduler.py
    settings.py
  docker/
    mysql-init.d/
  tests/
  doc/
  docker-compose.yml
  Dockerfile
  README.md
```

## 5. 基础设施选型

### 5.1 MVP 推荐栈

| 模块 | 推荐实现 | 说明 |
|---|---|---|
| API 服务 | FastAPI | 轻量、异步友好、便于 OpenAPI 输出 |
| 异步队列 | RabbitMQ | 与现有 trustguard-agent 项目一致 |
| 运行状态 / 缓存 | Redis | job heartbeat、query cache、限流 |
| 元数据存储 | MySQL | 复用现有基础设施 |
| 向量数据库 | Qdrant | 现有项目已使用，适合快速落地 |
| 全文检索 | OpenSearch | 补齐 BM25、精确关键词、highlight |
| 对象存储 | MinIO | 保存 raw HTML、PDF、clean text、大文档 |
| Embedding | **冻结为单一模型**（推荐 `bge-m3`，1024 维，中英混合安全术语表现好、可本地零成本；无 GPU 时退回 OpenAI-compatible `text-embedding-3-large`，3072 维） | 见下方"嵌入模型冻结"说明 |
| Rerank | bge-reranker-v2-m3 / Jina Reranker | 支持本地化或 API 模式 |

> **嵌入模型冻结（独立平台必须先定）**：向量维度由模型决定，**一旦建库就不能随意换模型**，否则已嵌向量全部作废、需要全量重嵌。因此本平台启动前先**冻结一个嵌入模型，把维度写进 Qdrant 集合配置**，并将模型名与维度记入集合/文档 metadata，便于审计与日后迁移。本平台是全新独立库，**不复用 trustguard-agent 现有 `text-embedding-3-small`（1536 维）的向量**，因此本阶段无需考虑两边维度兼容（对接阶段再统一）。

### 5.2 为什么必须加 OpenSearch

只用 Qdrant 会在以下场景出现明显短板：

- CVE ID、CWE ID、CPE、版本号、端口、路径、HTTP 参数等精确符号匹配。
- Nuclei template id、Metasploit module path、工具参数名。
- 报错字符串、函数名、配置项、文件路径。
- 中文/英文混合安全术语。

因此检索层应始终包含：

```text
Qdrant semantic retrieval
OpenSearch lexical retrieval
Metadata / structured retrieval
```

## 6. 数据采集与清洗

### 6.1 数据源分层

| 类型 | 数据源示例 | 入库价值 |
|---|---|---|
| 漏洞情报 | NVD、CVE、CWE、CAPEC、EPSS | 漏洞定位、影响判断、修复建议 |
| 攻击技术 | MITRE ATT&CK、OWASP、HackTricks | 阶段策略、技术解释 |
| 工具知识 | Nuclei、Metasploit、Nmap NSE、ffuf、sqlmap | 工具选择与参数建议 |
| 靶场知识 | Vulhub、项目靶场复现文档 | PoC、复现步骤、验证方式 |
| 安全公告 | 厂商公告、GitHub Security Advisories | 补丁版本、缓解措施 |
| 项目经验 | 扫描结果、执行轨迹、报告发现项 | 经验复用、智能体自我改进 |
| 人工知识 | 管理员录入、团队 SOP | 高可信规则和内部流程 |

> **CVE 全量入库的范围控制（重要，直接影响开发与演示速度）**：`data/` 下的 `cvelistV5-main.zip` 是 CVE List V5 全量（数十万条），**不要整库向量化**——既不经济（embedding 成本/时长高）也无必要。分两条路处理：
> - **CVE 结构化数据**（编号、CVSS、受影响产品/版本、参考链接）走**结构化检索 + BM25**，只入 metadata DB 与全文索引，**不嵌入**。
> - **只对 PoC、复现文档、安全公告、blog**（vulhub README、Nuclei 模板说明等）走**向量嵌入**。
> - MVP 先按 **vulhub 覆盖的 CVE + 演示目标相关产品**裁剪一个小集合跑通，后续再扩。

### 6.2 采集流程

```text
source_config
  -> crawl_job
  -> raw_document
  -> parsed_document
  -> cleaned_document
  -> chunks
  -> embeddings
  -> vector index + lexical index + metadata DB
```

### 6.3 清洗规则

通用清洗：

- 去除导航栏、页脚、广告、社交按钮、cookie banner。
- 去除重复版权声明、免责声明、无意义目录。
- 保留标题层级、列表、表格、代码块、命令行、HTTP 请求响应。
- 统一换行、空白、全角半角、异常编码。

安全领域清洗：

- 保留 `GET /path HTTP/1.1`、header、body、curl 命令、nuclei request、yaml 规则。
- 保留漏洞影响版本、修复版本、利用条件、检测条件。
- 提取并规范化 CVE/CWE/CPE、产品名、版本号、端口、协议、路径、参数。
- 对疑似密钥、token、cookie 做脱敏，不进入可检索正文。

去重策略：

```text
URL canonical
+ content hash
+ SimHash / MinHash
+ source priority
```

同一 CVE 多来源可以保留，但需要通过 `source_trust` 和 `source_type` 区分，避免重复 chunk 淹没结果。

## 7. 语义分块方案

吸收 RAGFlow 的 chunk enrichment 与 Dify 的 parent-child index 思路，采用三层结构：

```text
parent_chunk: 章节级，800-1500 tokens
child_chunk: 检索级，180-350 tokens
atomic_fact: 漏洞事实、PoC、修复、命令、HTTP 请求响应
```

### 7.1 parent_chunk

用途：

- 保留完整上下文。
- 在线检索命中 child 后回填 parent。
- 给报告生成提供更完整证据。

字段：

```json
{
  "chunk_type": "parent",
  "title_path": ["Tomcat", "CVE-2017-12615", "复现步骤"],
  "content": "...",
  "token_count": 1200
}
```

### 7.2 child_chunk

用途：

- 作为 Qdrant / OpenSearch 的主要检索单元。
- 尽量短而语义完整。

字段：

```json
{
  "chunk_type": "child",
  "parent_chunk_id": "...",
  "content": "...",
  "entities": {
    "cve_ids": ["CVE-2017-12615"],
    "products": ["tomcat"],
    "ports": [8080]
  }
}
```

### 7.3 atomic_fact

用于安全事实检索，例如：

```json
{
  "chunk_type": "atomic_fact",
  "fact_type": "fix_version",
  "subject": "Apache Tomcat",
  "predicate": "fixed_in",
  "object": "7.0.82 / 8.0.47 / 8.5.23"
}
```

适合：

- CVE 影响版本。
- 修复版本。
- Exploit 前置条件。
- 检测命令。
- HTTP request / response。
- Nuclei template rule。

### 7.4 question chunk

参考 RAGFlow 中 `question_kwd` 思路，为 chunk 生成“它能回答的问题”：

```text
这个漏洞影响哪些版本？
如何验证 Tomcat PUT 上传 JSP 漏洞？
该漏洞的修复版本是什么？
```

问题文本可以单独进入 OpenSearch，也可以作为 embedding 文本的一部分，提升问答检索命中率。

> **MVP 简化建议（为尽快可用）**：`atomic_fact`（7.3，三元组抽取）和 `question chunk`（7.4，问题生成）都强依赖 LLM 抽取、成本与质量风险较高。**MVP 先只做 parent-child 两层**把链路跑通，原子事实与问题生成放到 M3+ 再加，避免拖慢独立平台的首次可用。

## 8. 元数据与索引模型

### 8.1 关系型表

#### rag_documents

```sql
CREATE TABLE rag_documents (
  id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(128),
  project_id VARCHAR(128),
  source_type VARCHAR(64) NOT NULL,
  source_url TEXT,
  title TEXT,
  content_hash VARCHAR(128),
  raw_object_key TEXT,
  clean_object_key TEXT,
  source_trust FLOAT DEFAULT 0.5,
  status VARCHAR(32) NOT NULL,
  fetched_at DATETIME,
  parsed_at DATETIME,
  indexed_at DATETIME,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);
```

#### rag_chunks

```sql
CREATE TABLE rag_chunks (
  id VARCHAR(64) PRIMARY KEY,
  document_id VARCHAR(64) NOT NULL,
  parent_chunk_id VARCHAR(64),
  chunk_type VARCHAR(64) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  token_count INT,
  entities_json JSON,
  metadata_json JSON,
  source_start INT,
  source_end INT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);
```

#### rag_jobs

```sql
CREATE TABLE rag_jobs (
  id VARCHAR(64) PRIMARY KEY,
  job_type VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL,
  current_step VARCHAR(64),
  payload_json JSON,
  error TEXT,
  retry_count INT DEFAULT 0,
  created_at DATETIME NOT NULL,
  started_at DATETIME,
  finished_at DATETIME
);
```

### 8.2 Qdrant payload

Qdrant 只放检索过滤、排序、引用所需字段，不放超长正文：

```json
{
  "chunk_id": "chk_xxx",
  "document_id": "doc_xxx",
  "parent_chunk_id": "chk_parent_xxx",
  "tenant_id": "default",
  "project_id": "demo",
  "kb_tier": "cve",
  "phase": "VULN_SCAN",
  "source_type": "nvd",
  "source_url": "https://nvd.nist.gov/vuln/detail/CVE-...",
  "source_trust": 0.9,
  "cve_ids": ["CVE-2017-12615"],
  "cwe_ids": ["CWE-434"],
  "products": ["tomcat"],
  "versions": ["8.5"],
  "ports": [8080],
  "tags": ["rce", "file-upload"],
  "updated_at": "2026-06-07T00:00:00Z"
}
```

> **受影响版本区间需专门字段**：Qdrant payload / 关键词只能做**精确匹配**，无法回答"8.5.19 是否受影响（修复于 8.5.23）"这类**范围判断**——而这正是判定可利用性的关键。建议在结构化检索侧为受影响版本单独建字段（如 `version_start` / `version_end` / `version_op`），由 metadata DB 做范围查询，不要只靠 payload 里的 `versions` 数组。

### 8.3 OpenSearch document

OpenSearch 保留可全文检索字段：

```json
{
  "chunk_id": "chk_xxx",
  "document_id": "doc_xxx",
  "parent_chunk_id": "chk_parent_xxx",
  "title": "Apache Tomcat CVE-2017-12615",
  "content": "...",
  "question_text": "...",
  "important_keywords": ["tomcat", "PUT", "JSP", "CVE-2017-12615"],
  "cve_ids": ["CVE-2017-12615"],
  "products": ["tomcat"],
  "phase": "EXPLOIT",
  "tenant_id": "default",
  "project_id": "demo",
  "source_url": "..."
}
```

## 9. 异步任务设计

### 9.1 队列拆分

```text
rag.crawl
rag.parse
rag.clean
rag.chunk
rag.embed
rag.index
rag.refresh
rag.delete
```

### 9.2 Job 状态机

```text
PENDING
  -> RUNNING
  -> SUCCEEDED
  -> FAILED
  -> CANCELED
```

每个 job 记录：

```json
{
  "job_id": "job_xxx",
  "job_type": "url_ingest",
  "status": "RUNNING",
  "current_step": "chunk",
  "retry_count": 1,
  "payload": {
    "source_url": "https://example.com/security/advisory",
    "tenant_id": "default",
    "project_id": "demo"
  }
}
```

### 9.3 Worker 执行链

```text
POST /v1/ingest/jobs
  -> create rag_jobs row
  -> publish rag.crawl

rag.crawl worker
  -> fetch raw content
  -> save raw to MinIO
  -> publish rag.parse

rag.parse worker
  -> HTML/PDF/Markdown parser
  -> publish rag.clean

rag.clean worker
  -> boilerplate removal
  -> security text normalization
  -> dedup
  -> publish rag.chunk

rag.chunk worker
  -> parent-child chunking
  -> entity extraction
  -> question generation
  -> save chunks
  -> publish rag.embed

rag.embed worker
  -> batch embedding
  -> publish rag.index

rag.index worker
  -> upsert Qdrant
  -> upsert OpenSearch
  -> mark document indexed
```

### 9.4 幂等策略

- document id: `sha256(source_type + canonical_url + tenant_id + project_id)`
- chunk id: `sha256(document_id + parent_id + chunk_order + content_hash)`
- vector point id: 使用 chunk id 或其稳定 hash。
- 同一文档重复入库时，content hash 未变则跳过 parse/chunk/embed。

## 10. 检索增强链路

### 10.1 在线查询流程

```text
POST /v1/query
  -> validate tenant/project
  -> query analyze
  -> query rewrite
  -> vector retrieval
  -> lexical retrieval
  -> structured retrieval
  -> fusion and dedup
  -> rerank
  -> parent expansion
  -> context packing
  -> return hits and citations
```

### 10.2 Query Analyzer

从用户问题和 `target_context` 中提取：

```json
{
  "intent": "exploit_verification",
  "phase": "EXPLOIT",
  "entities": {
    "cve_ids": ["CVE-2017-12615"],
    "products": ["tomcat"],
    "ports": [8080],
    "http_methods": ["PUT"],
    "file_extensions": ["jsp"]
  },
  "query_type": "version_or_exploit_specific"
}
```

### 10.3 Query Rewrite

输入：

```text
Tomcat 8080 PUT 上传 JSP 如何验证和修复？
```

输出：

```json
{
  "semantic_query": "Apache Tomcat PUT method JSP upload vulnerability verification and remediation",
  "keyword_query": "tomcat 8080 PUT JSP upload CVE-2017-12615 fix version",
  "expanded_queries": [
    "Tomcat PUT file upload RCE",
    "CVE-2017-12615 affected versions fixed versions",
    "Tomcat readonly false PUT JSP exploit"
  ],
  "metadata_filters": {
    "phase": ["EXPLOIT", "VULN_SCAN"],
    "products": ["tomcat"],
    "ports": [8080]
  }
}
```

查询改写分三层：

1. 规则改写：CVE、端口、URL、产品、版本、文件路径等。
2. 词表扩展：安全术语同义词、工具名、漏洞类别。
3. LLM 改写：用于复杂问题，生成 `semantic_query`、`keyword_query`、`sub_questions`。

MVP 可以先做规则 + 词表，后续再接 LLM。

### 10.4 Hybrid Retrieval

向量检索：

```text
Qdrant.search(
  vector=embedding(semantic_query),
  filter=tenant/project/phase/entities,
  top_k=80
)
```

字面检索：

```text
OpenSearch BM25(
  query=keyword_query,
  fields=[
    title^4,
    cve_ids^5,
    products^3,
    important_keywords^3,
    question_text^2,
    content
  ],
  top_k=80
)
```

结构化检索：

```text
metadata DB lookup:
  CVE exact match
  product + version match
  port/service match
  phase/tier/source_trust filtering
```

### 10.5 Fusion

采用 RRF 作为 MVP 融合策略：

```text
rrf_score = sum(1 / (k + rank_i))
```

默认 `k=60`。

再叠加领域权重：

```text
final_fusion_score =
  rrf_score
  * phase_factor
  * source_trust_factor
  * tenant_scope_factor
  * freshness_factor
```

不同 query 类型可动态调权：

| 查询类型 | vector | BM25 | metadata |
|---|---:|---:|---:|
| 概念解释 | 0.65 | 0.25 | 0.10 |
| CVE / 版本 / 路径 | 0.30 | 0.45 | 0.25 |
| 工具使用 | 0.45 | 0.40 | 0.15 |
| 修复建议 | 0.45 | 0.25 | 0.30 |
| 报告生成 | 0.40 | 0.25 | 0.35 |

### 10.6 Rerank

候选集合：

```text
fusion top 80 -> rerank top 12
```

Rerank 输入：

```json
{
  "query": "Tomcat 8080 PUT 上传 JSP 如何验证和修复？",
  "documents": [
    "chunk text 1",
    "chunk text 2"
  ]
}
```

MVP 模型：

- 本地：`BAAI/bge-reranker-v2-m3`
- API：Jina Reranker / Cohere Rerank

Rerank 后保留：

```json
{
  "fusion_score": 0.62,
  "rerank_score": 0.91,
  "vector_score": 0.78,
  "bm25_score": 12.4,
  "metadata_score": 0.8
}
```

### 10.7 Parent Expansion

如果命中 child chunk：

```text
child chunk -> parent_chunk_id -> load parent chunk -> context pack
```

返回时同时给：

- child content：精确命中证据。
- parent preview：完整上下文。
- citation：来源文档与 URL。

## 11. API 设计

### 11.1 创建入库任务

```http
POST /v1/ingest/jobs
```

请求：

```json
{
  "source_type": "url",
  "source": "https://example.com/security/advisory",
  "tenant_id": "default",
  "project_id": "demo",
  "kb_tier": "blog",
  "phase": "VULN_SCAN",
  "tags": ["tomcat", "rce"]
}
```

响应：

```json
{
  "job_id": "job_xxx",
  "status": "PENDING"
}
```

### 11.2 查询任务状态

```http
GET /v1/ingest/jobs/{job_id}
```

响应：

```json
{
  "job_id": "job_xxx",
  "status": "RUNNING",
  "current_step": "embed",
  "retry_count": 0,
  "error": null
}
```

### 11.3 检索接口

```http
POST /v1/query
```

请求：

```json
{
  "query": "Tomcat 8080 PUT 上传 JSP 如何验证和修复？",
  "phase": "EXPLOIT",
  "tenant_id": "default",
  "project_id": "demo",
  "target_context": {
    "services": [
      {
        "port": 8080,
        "product": "tomcat"
      }
    ]
  },
  "top_k": 12,
  "include_parent": true,
  "include_debug": true
}
```

响应：

```json
{
  "query_id": "qry_xxx",
  "rewritten_queries": {
    "semantic_query": "...",
    "keyword_query": "...",
    "expanded_queries": []
  },
  "hits": [
    {
      "chunk_id": "chk_xxx",
      "document_id": "doc_xxx",
      "parent_chunk_id": "chk_parent_xxx",
      "content": "...",
      "parent_preview": "...",
      "score": 0.91,
      "fusion_score": 0.68,
      "rerank_score": 0.91,
      "vector_score": 0.77,
      "bm25_score": 13.2,
      "source_url": "https://...",
      "source_type": "nvd",
      "kb_tier": "cve",
      "phase": "EXPLOIT",
      "cve_ids": ["CVE-2017-12615"],
      "metadata": {}
    }
  ],
  "citations": [
    {
      "chunk_id": "chk_xxx",
      "source_url": "https://...",
      "title": "..."
    }
  ]
}
```

### 11.4 Search-only 接口

```http
POST /v1/search
```

用于前端调试检索，不做 context pack，可返回更多 debug 信息。

### 11.5 Chunk 读取接口

```http
GET /v1/chunks/{chunk_id}
```

用于报告生成、调试、引用回看。

## 12. 安全与权限设计

### 12.1 多租户隔离

所有检索必须默认附带：

```json
{
  "tenant_id": "...",
  "project_id": "..."
}
```

如果请求没有明确 tenant/project：

- 开发环境可使用默认 tenant。
- 生产环境必须拒绝或由上游鉴权注入。

### 12.2 爬虫安全

爬虫模块必须实现：

- URL allowlist / denylist。
- 禁止访问内网地址、云 metadata 地址、本机地址，防 SSRF。
- 请求超时、最大响应体积限制。
- robots 与授权记录按项目策略配置。
- User-Agent 标识。
- 失败重试上限。

### 12.3 敏感信息处理

入库前执行 secret detection：

- API key。
- JWT。
- Cookie。
- Authorization header。
- 私钥。
- 数据库连接串。

策略：

```text
可公开安全知识: 直接入库
内部报告/执行轨迹: 脱敏后入库
疑似密钥: 不入库，记录审计事件
```

## 13. 与现有 trustguard-agent 的接入方案

> **本节后置，本阶段不实施。** 以下仅作为日后对接的参考占位，独立平台开发阶段**无需为此做任何特殊处理**——本平台自己用什么 chunk_id 格式、什么租户字段都可以，对接时再统一。已核对现有代码，对接开工前至少需先解决两处兼容性约束：
> - **chunk_id 格式**：现有 orchestrator 强校验 `context_chunk_refs` 的 chunk_id 必须 `chk-` 前缀 + 后缀 ≥16 位字母数字、且必须出现在本次 `kb_hits` 中（见 `orchestrator-service/app/core/plan_business_validate.py` 与 `chunk_store.py` 的 `CHUNK_ID_PREFIX = "chk-"`）。本平台对接时需把 chunk_id 统一成该格式，或在 `RagHttpClient` 做一层 ID 映射，否则 LLM 引用即被 Plan 校验拒绝。
> - **租户字段映射**：现有 `kb_hits` 用 `workspace_id` + `project_id`，本平台用 `tenant_id` + `project_id`，对接时需约定 `tenant_id ↔ workspace_id` 映射，避免隔离失效。
> - 现有系统还有经验回流能力（`propose_experience` / experience promotion），对接阶段需在本平台补 experience 写入 + gating 接口，本文档第 11 节暂未包含。

### 13.1 当前接入点

现有项目中，RAG 注入主要位于：

```text
trustguard-agent/trustguard-agent/orchestrator-service/app/clients/llm_client.py
```

当前调用链大致为：

```text
llm_client
  -> _build_kb_query_and_filters
  -> get_kb_client()
  -> QdrantKBClient.retrieve()
  -> target_context["kb_hits"]
```

后续改为：

```text
llm_client
  -> RagHttpClient.query()
  -> POST http://rag-service:18200/v1/query
  -> target_context["kb_hits"]
```

### 13.2 新增配置

```env
RAG_SERVICE_ENABLED=true
RAG_SERVICE_URL=http://trustguard-rag-platform:18200
RAG_SERVICE_TIMEOUT_SECONDS=30
RAG_SERVICE_TOP_K=12
RAG_SERVICE_INCLUDE_DEBUG=false
```

### 13.3 RagHttpClient 契约

```python
class RagHttpClient:
    async def query(
        self,
        *,
        query: str,
        phase: str,
        tenant_id: str | None,
        project_id: str | None,
        target_context: dict,
        top_k: int,
    ) -> list[dict]:
        ...
```

返回的 hit 转换为现有 `kb_hits`：

```json
{
  "source": "rag-platform",
  "id": "chk_xxx",
  "score": 0.91,
  "snippet": "...",
  "chunk_id": "chk_xxx",
  "parent_chunk_id": "chk_parent_xxx",
  "source_url": "...",
  "cve_ids": ["CVE-..."],
  "skill_id": "nuclei",
  "workspace_id": "default"
}
```

### 13.4 兼容旧 KB

迁移期支持双读：

```text
if RAG_SERVICE_ENABLED:
    call independent RAG platform
else:
    use existing QdrantKBClient
```

也可以灰度：

```text
RAG_SERVICE_SHADOW_MODE=true
```

shadow mode 下：

- 主链路仍用旧 KB。
- 后台调用新 RAG。
- 对比 topK、命中率、耗时。
- trace 中记录差异。

## 14. 评测体系

### 14.1 检索评测集

构造 TrustGuard 专用 query set：

| 类型 | 示例 |
|---|---|
| CVE 精确 | `CVE-2017-12615 影响哪些 Tomcat 版本` |
| 产品版本 | `Tomcat 8.5 PUT JSP 上传怎么验证` |
| 工具使用 | `nuclei 如何检测 Spring4Shell` |
| 修复建议 | `Log4Shell 如何缓解和修复` |
| 阶段策略 | `发现 8080 tomcat 后下一步做什么` |
| 报告生成 | `根据证据生成漏洞描述和修复建议` |

每个 query 标注：

```json
{
  "query": "...",
  "must_hit_cve": ["CVE-..."],
  "must_hit_source": ["nvd", "vulhub"],
  "expected_chunk_ids": [],
  "phase": "EXPLOIT"
}
```

### 14.2 指标

检索指标：

- Recall@5 / Recall@10
- MRR
- nDCG@10
- Exact entity hit rate
- CVE hit rate
- Source citation accuracy

生成指标：

- Faithfulness
- Citation coverage
- Hallucination rate
- 修复建议可执行性

工程指标：

- P95 query latency
- ingest job success rate
- embedding cost
- rerank latency
- queue backlog

## 15. 实施里程碑

### M0：项目骨架

交付：

- FastAPI 服务。
- Dockerfile / docker-compose。
- `/health`。
- MySQL / Qdrant / OpenSearch / RabbitMQ 连接检查。

验收：

- 服务可独立启动。
- OpenAPI 可访问。
- health 返回各依赖状态。

### M1：最小入库链路

交付：

- `POST /v1/ingest/jobs`。
- URL / Markdown 入库。
- raw / clean 存储。
- parent-child chunk。
- Qdrant + OpenSearch 双写。

验收：

- 给定一个 Vulhub README 或安全公告 URL，可以异步入库。
- Qdrant 与 OpenSearch 均可查到对应 chunk。

### M2：基础混合检索

交付：

- `/v1/query`。
- query analyzer。
- Qdrant vector retrieval。
- OpenSearch BM25 retrieval。
- RRF fusion。
- parent expansion。

验收：

- CVE / 产品 / 阶段相关 query 能返回引用。
- 返回 vector_score、bm25_score、fusion_score。

### M3：高级检索增强

交付：

- query rewrite。
- rerank。
- metadata structured lookup。
- source trust / phase boost。
- debug trace。

验收：

- 与单向量检索相比，Recall@10 和 MRR 有明显提升。
- 精确 CVE query 不被泛语义结果挤掉。

### M4：接入 trustguard-agent（后置，本阶段不实施）

> 本里程碑整体后置到独立平台稳定、评测通过之后再启动，详见第 13 节的兼容性约束。以下为占位。

交付：

- `RagHttpClient`。
- `RAG_SERVICE_ENABLED` feature flag。
- `llm_client.py` 中接入新 RAG 服务。
- shadow mode 对比。

验收：

- orchestrator 能把新 RAG 命中写入 `target_context["kb_hits"]`。
- PlanItem 仍可引用真实 `chunk_id`。
- 旧 KB 可通过开关回退。

### M5：评测与展示

交付：

- 检索评测集。
- 评测脚本。
- 指标报告。
- Demo 数据集：NVD + Vulhub + Nuclei + 手工知识。

验收：

- 可演示“无 RAG / 单向量 RAG / 混合检索 + rerank RAG”效果差异。
- 可展示引用溯源与任务异步状态。

## 16. 风险与应对

| 风险 | 表现 | 应对 |
|---|---|---|
| 只做向量检索导致错召回 | CVE、版本、路径命中不稳定 | OpenSearch + metadata lookup |
| rerank 延迟过高 | 查询 P95 变大 | fusion topK 控制、GPU、本地/云切换、缓存 |
| 入库任务失败难排查 | 文档卡在半入库 | job step 状态、error 落库、重试与死信 |
| chunk 太碎 | 答案缺上下文 | parent-child expansion |
| chunk 太大 | 检索不准、rerank 慢 | child chunk 控制在 180-350 tokens |
| 内部报告泄露 | 跨项目查到敏感信息 | tenant/project 强过滤、脱敏、classification |
| OpenSearch 与 Qdrant 不一致 | 删除/更新后结果漂移 | 统一 index job、索引版本、reindex API |

## 17. 最终形态

最终系统应该形成两条闭环：

### 17.1 知识生产闭环

```text
数据源
  -> 异步采集
  -> 清洗解析
  -> 结构化抽取
  -> 父子分块
  -> embedding
  -> Qdrant + OpenSearch + metadata
  -> 评测与监控
```

### 17.2 智能体使用闭环

```text
TrustGuard Agent / Orchestrator
  -> RAG query
  -> query rewrite
  -> hybrid retrieval
  -> rerank
  -> citations
  -> 决策 / 工具调用 / 报告生成
  -> 执行经验回流 RAG
```

## 18. 近期开发优先级

建议按以下顺序开工：

1. 建立 `trustguard-rag-platform` FastAPI 骨架与 docker compose。
2. 增加 MySQL schema、Qdrant、OpenSearch 连接。
3. 实现 Markdown / URL 入库最小链路。
4. 实现 Qdrant + OpenSearch 双写。
5. 实现 `/v1/query` 的 vector + BM25 + RRF。
6. 加入 rerank。
7. 建立评测集和 demo 数据，跑出"无 RAG / 单向量 / 混合检索 + rerank"的对比效果。
8. （后置）做 `RagHttpClient` 接入现有 orchestrator——独立平台稳定且评测通过后再启动。

## 19. 一句话总结

本方案不是把现有 Qdrant KB 稍微包装一下，而是把 RAG 能力从 orchestrator 中抽离成独立平台：采集、清洗、分块、向量化、全文索引、混合检索、rerank、引用与权限全部在独立服务内闭环。现有 trustguard-agent 项目只消费稳定 API，从而获得更清晰的工程边界、更强的检索质量和更好的比赛展示效果。
