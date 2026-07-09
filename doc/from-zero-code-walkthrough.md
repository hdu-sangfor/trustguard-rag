# 从 0 理解 trustguard-rag-platform 代码攻略

> 目标：不要一上来读完整 RAG 平台，而是按可运行的最小单元逐步搭起来。每一步都先理解“数据怎么流动”，再看具体实现。

## 0. 先建立全局地图

当前项目主链路：

```text
用户 / API
  -> FastAPI 路由
  -> MySQL 记录 job / document / chunk
  -> RabbitMQ 投递入库任务
  -> rag-worker 消费任务
  -> crawl / parse / clean / chunk / extract / embed / index
  -> Qdrant + OpenSearch
  -> /v1/query 混合检索
```

先记住一句话：

```text
API 负责接请求和投递任务；worker 负责真正干重活；query 负责从索引里查回来。
```

## 1. 第一阶段：FastAPI 空壳

目标：只理解服务怎么启动，不碰数据库、队列、向量库。

重点文件：

```text
app/main.py
app/settings.py
app/api/health.py
app/schemas/api.py
```

你从 0 写时先实现：

```text
GET /
GET /health/live
```

理解顺序：

```text
uvicorn app.main:app
  -> create_app()
  -> include_router()
  -> /health/live
```

第一阶段只测：

```text
http://localhost:18200/health/live
```

如果返回：

```json
{"status": "alive"}
```

就算过关。

不要一开始看 `/health`。`/health` 会检查 MySQL、Qdrant、OpenSearch、Redis、RabbitMQ，依赖没启动时出现 `down` 是正常的。

## 2. 第二阶段：配置系统

目标：理解 `.env` / 环境变量怎么进代码。

重点文件：

```text
app/settings.py
.env.example
docker-compose.yml
```

核心概念：

```text
Settings(BaseSettings)
  -> 读取 RAG_ 前缀变量
  -> 生成 mysql_dsn / qdrant_url / redis_url / rabbitmq_url
```

你需要知道这些配置：

```text
RAG_MYSQL_HOST / RAG_MYSQL_PORT
RAG_QDRANT_HOST / RAG_QDRANT_PORT
RAG_OPENSEARCH_HOST / RAG_OPENSEARCH_PORT
RAG_REDIS_HOST / RAG_REDIS_PORT
RAG_RABBITMQ_HOST / RAG_RABBITMQ_PORT
RAG_EMBEDDING_DIM
```

先不要纠结每个依赖怎么用，只要知道配置从这里统一管理。

## 3. 第三阶段：API Schema

目标：先理解接口数据长什么样。

重点文件：

```text
app/schemas/document.py
app/schemas/job.py
app/schemas/chunk.py
app/schemas/retrieval.py
```

核心对象：

```text
IngestJobRequest   入库请求
IngestJobResponse  入库任务响应
Job                后台任务状态
Chunk              分块
QueryRequest       检索请求
Hit                检索命中
Citation           引用
```

这一阶段只读字段，不看业务。

## 4. 第四阶段：MySQL Store

目标：理解项目怎么把状态落库。

重点文件：

```text
docker/mysql-init.d/001_init.sql
app/stores/db.py
app/stores/job_store.py
app/stores/document_store.py
app/stores/chunk_store.py
```

三张核心表：

```text
rag_jobs       记录任务状态
rag_documents 记录文档元数据
rag_chunks    记录 parent / child chunk 正文
```

理解顺序：

```text
JobStore.create()
JobStore.update()
JobStore.get()

DocumentStore.upsert()
DocumentStore.set_status()

ChunkStore.bulk_upsert()
ChunkStore.get_many()
```

这一步的重点不是 SQLAlchemy，而是状态：

```text
PENDING -> RUNNING -> SUCCEEDED / FAILED
```

## 5. 第五阶段：RabbitMQ 接入

目标：理解 API 和后台 worker 怎么解耦。

重点文件：

```text
app/api/ingest.py
app/stores/rabbitmq.py
app/workers/worker.py
app/workers/handlers.py
docker-compose.yml
```

当前真实链路：

```text
POST /v1/ingest/jobs
  -> JobStore.create("ingest", payload)
  -> publish_ingest_job(job_id)
  -> RabbitMQ queue: rag.ingest
  -> app.workers.worker 消费消息
  -> run_ingest_job(job_id)
```

注意：现在已经不是 `BackgroundTasks` 了。

本地开发需要两个终端：

```bash
uvicorn app.main:app --reload --port 18200
```

```bash
python -m app.workers.worker
```

Docker 方式会自动启动：

```text
rag-service
rag-worker
rabbitmq
```

理解 RabbitMQ 时先记一句：

```text
消息里只放 job_id，真正的任务详情从 MySQL 的 rag_jobs.payload_json 读取。
```

## 6. 第六阶段：入库流水线

目标：理解一个文档怎么变成可检索 chunk。

重点文件：

```text
app/workers/handlers.py
```

核心函数：

```text
run_ingest_job(job_id)
```

流水线：

```text
crawl
  -> parse
  -> clean
  -> chunk
  -> extract entities
  -> embed
  -> index
```

对应代码：

```text
_crawl()
_parse()
security_text.clean()
parent_child.chunk()
security_entities.extract()
get_embedding_provider().embed()
qdrant_indexer.upsert()
opensearch_indexer.index_chunks()
```

建议从最简单的 Markdown 入库开始理解：

```json
{
  "source_type": "markdown",
  "source": "# Tomcat\n\nCVE-2017-12615 affects Apache Tomcat ..."
}
```

不要一开始就用 PDF、网页、真实 embedding。

## 7. 第七阶段：Crawler 与 Parser

目标：理解“原始输入”怎么变成统一文本。

重点文件：

```text
app/core/crawler/local_file.py
app/core/crawler/web.py
app/core/parser/markdown_parser.py
app/core/parser/html_parser.py
app/core/parser/json_parser.py
app/core/parser/pdf_parser.py
app/core/parser/docx_parser.py
```

入口：

```text
local_file.read_path()
web.fetch()
handlers._parse()
```

当前支持：

```text
markdown / text
url
local_file: .md / .txt / .html / .json / .pdf / .docx
```

PDF：

```text
pdfplumber
  -> 按页抽取文本
  -> 表格转 Markdown table
  -> 输出 ## Page N
```

DOCX：

```text
python-docx
  -> 段落
  -> 标题样式转 Markdown heading
  -> 表格转 Markdown table
  -> 简单过滤页眉页脚
```

MinerU 的位置：

```text
现在不强依赖 MinerU。
后续可以做成 PDF 高级解析 provider：

RAG_PDF_PARSER=basic
RAG_PDF_PARSER=mineru
```

建议演进结构：

```text
app/core/parser/
  pdf_parser.py          # 分发入口
  pdf_basic_parser.py    # pdfplumber
  pdf_mineru_parser.py   # MinerU
```

## 8. 第八阶段：清洗、实体抽取、分块

目标：理解 RAG 的“文档加工”。

重点文件：

```text
app/core/cleaner/security_text.py
app/core/cleaner/boilerplate.py
app/core/cleaner/dedup.py
app/core/extractor/security_entities.py
app/core/chunker/parent_child.py
```

清洗：

```text
统一换行
去多余空白
脱敏 Authorization / API key / JWT / 私钥
```

实体抽取：

```text
CVE
CWE
产品名
端口
版本号
```

分块结构：

```text
document
  -> parent chunk  章节级上下文
  -> child chunk   检索单元
```

最重要的一点：

```text
Qdrant / OpenSearch 主要索引 child chunk。
命中 child 后，再通过 parent_chunk_id 回填 parent preview。
```

## 9. 第九阶段：Embedding

目标：理解向量从哪里来。

重点文件：

```text
app/core/embedding/provider.py
app/core/embedding/openai_compatible.py
```

当前逻辑：

```text
如果配置 RAG_EMBEDDING_BASE_URL:
  使用 OpenAI-compatible embedding 接口
否则:
  回退 DummyEmbeddingProvider
```

DummyEmbeddingProvider 只用于打通链路：

```text
确定性
无需外部模型
没有真实语义效果
```

所以早期理解代码可以放心用 dummy。等链路通了，再接 bge-m3 或其他真实 embedding。

## 10. 第十阶段：双索引

目标：理解为什么要 Qdrant + OpenSearch。

重点文件：

```text
app/core/indexer/qdrant_indexer.py
app/core/indexer/opensearch_indexer.py
app/stores/qdrant_store.py
app/stores/opensearch_store.py
```

Qdrant：

```text
语义向量检索
适合概念相近的问题
```

OpenSearch：

```text
BM25 / 关键词检索
适合 CVE、版本号、路径、参数、产品名
```

安全场景必须有 BM25，因为：

```text
CVE-2017-12615
/manager/html
8.5.23
PUT
JSP
```

这些精确符号不能只靠向量。

## 11. 第十一阶段：查询链路

目标：理解 `/v1/query` 怎么返回结果。

重点文件：

```text
app/api/query.py
app/core/retrieval/service.py
app/core/retrieval/query_analyzer.py
app/core/retrieval/query_rewriter.py
app/core/retrieval/vector_retriever.py
app/core/retrieval/lexical_retriever.py
app/core/retrieval/fusion.py
app/core/retrieval/parent_expander.py
app/core/context_pack/packer.py
app/core/context_pack/citation.py
```

完整流程：

```text
POST /v1/query
  -> analyze
  -> rewrite
  -> vector retrieval
  -> lexical retrieval
  -> RRF fusion
  -> ChunkStore.get_many()
  -> parent expansion
  -> build hits
  -> build citations
```

RRF 融合一句话：

```text
同时被向量和 BM25 命中的 chunk 会排得更靠前。
```

## 12. 当前还没真正完成的能力

这些文件是占位或 M3+：

```text
app/core/retrieval/structured_retriever.py
app/core/rerank/base.py
app/core/rerank/bge_reranker.py
app/core/rerank/jina_reranker.py
app/core/chunker/atomic_fact.py
app/core/chunker/qa_chunk.py
app/core/extractor/question_generator.py
app/core/embedding/local_bge.py
```

不要把它们当成现在已可用的功能。

当前真实可理解的闭环是：

```text
Markdown / URL / local_file
  -> parent-child chunk
  -> dummy or OpenAI-compatible embedding
  -> Qdrant + OpenSearch
  -> hybrid query
```

## 13. 推荐重建顺序

如果你想从 0 手写一遍，按这个节奏：

```text
Day 1:
  main.py / settings.py / health.py

Day 2:
  schemas
  /docs 能看到 OpenAPI

Day 3:
  docker/mysql-init.d
  db.py
  JobStore

Day 4:
  POST /v1/ingest/jobs
  先只创建 job，不执行任务

Day 5:
  RabbitMQ publish
  worker consume job_id

Day 6:
  run_ingest_job
  只支持 markdown

Day 7:
  cleaner
  parent_child chunker
  security_entities

Day 8:
  ChunkStore / DocumentStore
  把 chunk 写入 MySQL

Day 9:
  DummyEmbeddingProvider
  Qdrant indexer

Day 10:
  OpenSearch indexer
  BM25 检索

Day 11:
  /v1/query
  vector + lexical + RRF

Day 12:
  parser 扩展：html / json / pdf / docx

Day 13:
  真实 embedding
  准备 demo 数据
```

## 14. 最小调试路线

启动依赖：

```bash
docker compose up -d mysql redis rabbitmq qdrant opensearch
```

启动 API：

```bash
uvicorn app.main:app --reload --port 18200
```

启动 worker：

```bash
python -m app.workers.worker
```

检查：

```text
GET http://localhost:18200/health/live
GET http://localhost:18200/health
GET http://localhost:18200/docs
```

创建入库任务：

```bash
curl -X POST http://localhost:18200/v1/ingest/jobs \
  -H "Content-Type: application/json" \
  -d '{"source_type":"markdown","source":"# Tomcat\n\nCVE-2017-12615 affects Apache Tomcat.","tenant_id":"default","project_id":"default"}'
```

查询任务：

```text
GET /v1/ingest/jobs/{job_id}
```

检索：

```bash
curl -X POST http://localhost:18200/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query":"CVE-2017-12615 Tomcat","tenant_id":"default","project_id":"default","top_k":5}'
```

## 15. 读代码时的心法

不要按目录顺序读。

按数据流读：

```text
请求进来
  -> 变成 schema
  -> 写 job
  -> 进队列
  -> worker 拿 job
  -> 解析文档
  -> 分块
  -> 索引
  -> 查询
  -> 融合
  -> 返回引用
```

每次只追一个对象：

```text
job_id
document_id
chunk_id
parent_chunk_id
```

这四个 ID 追明白，整个项目就不会乱。
