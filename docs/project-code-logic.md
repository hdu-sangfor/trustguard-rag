# TrustGuard RAG 当前项目代码逻辑全景

> 文档快照：2026-07-18  
> 对应分支：`integration/ocr-mineru`  
> 对应提交：`f35465e`（`feat: 统一 PDF 解析流程并默认使用 MinerU`）  
> 事实来源：当前仓库源码、Docker Compose 配置和自动化测试。若本文与旧文档冲突，以当前源码为准。

本文面向第一次接触本项目的开发者，从“服务为什么存在”开始，逐层解释 HTTP API、异步任务、文件解析、MinerU、OCR、分块、Embedding、双索引、混合检索、数据存储、故障恢复、前端、Docker 和测试代码之间的实际关系。

## 1. 项目定位

TrustGuard RAG 是一个独立的知识库与检索服务，目前负责两类核心业务：

1. 将 PDF、DOCX、文本、Markdown、CSV、JSON、HTML 和图片转化为可检索的知识分块。
2. 同时使用 Qdrant 向量召回和 OpenSearch BM25 召回，再经过融合及可选 Rerank 返回带来源信息的知识片段。

项目目前没有 LLM 问答生成链路。`POST /v1/search` 返回的是检索片段、分数和引用来源，不负责基于这些片段生成最终自然语言答案。

从一致性角度看，MySQL 是业务状态的权威来源；MinIO/本地目录保存文件产物；Qdrant 和 OpenSearch 是可以由权威数据重建的派生索引；RabbitMQ 用于可靠异步执行。

## 2. 总体架构

```text
浏览器 / API 调用方
        │
        │ 上传、查询、搜索、管理
        ▼
rag-service / FastAPI
        ├──────────► MySQL
        ├──────────► MinIO / 本地 BlobStore
        ├──────────► outbox_events
        ├─健康检查► MinerU API
        └─健康检查► Redis

outbox_events ──Outbox relay──► RabbitMQ ──► rag-worker
                                                ├──► MySQL
                                                ├──► MinIO / BlobStore
                                                ├──► MinerU API
                                                ├──► Qdrant
                                                └──► OpenSearch
```

运行时组件如下：

| 组件 | 责任 | Compose 服务 | 宿主端口 |
|---|---|---|---:|
| FastAPI | HTTP API、静态前端、健康检查、启动回填 | `rag-service` | 18200 |
| Worker | Outbox 中继、RabbitMQ 消费、入库与清理 Saga | `rag-worker` | 无公开端口 |
| MinerU | PDF/DOCX 结构化解析，默认也承担扫描 PDF OCR | `mineru-api` | 18220 |
| MySQL | 文档、分块、任务、Outbox、OCR 区域的权威状态 | `mysql` | 18210 |
| Redis | 当前主要用于连通性探针和预留缓存客户端 | `redis` | 18211 |
| RabbitMQ | 入库、冲突解决、清理命令及延迟重试 | `rabbitmq` | 18212，管理页 18213 |
| Qdrant | 稠密向量索引 | `qdrant` | 18214，gRPC 18215 |
| OpenSearch | BM25 全文索引 | `opensearch` | 18216 |
| MinIO | 上传暂存、原始文件、抽取文本和 OCR 图片 | `minio` | 18217，控制台 18218 |

## 3. 仓库目录与职责

```text
app/
  main.py                         FastAPI 创建、路由注册、生命周期
  settings.py                     所有 RAG_ 环境变量定义与校验
  api/                            HTTP 路由层
  schemas/                        API 请求/响应 Pydantic 模型
  domain/                         稳定的领域枚举和状态值
  core/
    ingest/                       文件入库 Saga、抽取器、分块和补偿
    ocr/                          Paddle/API/custom OCR 与文本合并
    embedding/                    pseudo/local/API Embedding
    indexing/                     Qdrant/OpenSearch 写入和重建工具
    retrieval/                    向量/BM25 召回、融合、Rerank
  stores/                         MySQL、MinIO、本地存储和外部客户端
  workers/                        Outbox relay、RabbitMQ consumer、恢复循环
docker/mysql-init.d/              MySQL 初始化表结构
frontend/                         无框架的单页管理控制台
evaluation/cybersecurity/         网络安全检索评测集与评测脚本
tests/                            单元、集成和端到端测试
docs/                             专题设计与实现文档
Dockerfile                        RAG API/Worker 镜像
Dockerfile.mineru                 MinerU pipeline 镜像
docker-compose.yml                完整开发运行栈
```

推荐的源码阅读顺序：

1. `app/settings.py`
2. `app/main.py` 和 `app/api/`
3. `app/workers/messages.py`、`handlers.py`
4. `app/core/ingest/pipeline.py`
5. `app/core/ingest/extractors/`
6. `app/core/embedding/`、`indexing/`、`retrieval/`
7. `app/stores/` 和 `app/domain/`

## 4. 应用启动和生命周期

FastAPI 入口是 `app.main:app`。`create_app()` 完成以下工作：

- 注册 health、ingest、documents、sources、search、ocr-review 路由。
- 将 `frontend/assets` 挂载到 `/assets`。
- 根路径 `/` 直接返回 `frontend/index.html`。
- `/docs` 使用 FastAPI 自动生成的 OpenAPI 页面。

`lifespan()` 启动阶段按顺序执行：

1. 读取并缓存 `Settings`。
2. 初始化日志级别。
3. `ensure_outbox_schema()`：幂等创建 Outbox 表，并为旧数据库补充 Worker 租约字段和索引。
4. `ensure_ocr_schema()`：通过 SQLAlchemy `Base.metadata.create_all()` 补建尚不存在的 ORM 表。
5. 真实 OpenSearch 模式且开启启动回填时，把所有 MySQL `ready` 文档的 active chunks 幂等写回 OpenSearch。

关闭阶段释放 MySQL、Qdrant、OpenSearch 和 Redis 客户端。

注意：`create_all()` 只负责创建缺失表，不是完整数据库迁移系统；已有表的复杂字段变更仍应使用正式 migration 或手工 SQL。

## 5. 配置加载与覆盖关系

`Settings` 使用 `pydantic-settings`，统一采用 `RAG_` 前缀，并读取仓库根目录 `.env`。

有效值的常见优先关系是：

```text
Settings 类中的代码默认值
  < .env / env_file
  < 进程环境变量
  < docker-compose.yml 中 service.environment 的显式值
```

因此需要区分三套“默认”：

- 源码默认：方便轻依赖开发，例如 `embedding_provider=pseudo`、检索后端 mock。
- `.env.example`：面向完整本地部署，例如本地 Qwen Embedding、真实 OpenSearch。
- Compose 强制值：容器内使用服务名，并强制启用 MySQL、MinIO、Qdrant、OpenSearch、MinerU。

生产环境有一条显式保护：当 `RAG_APP_ENV=prod` 时，如果 Qdrant 或 OpenSearch 仍为 mock，配置校验直接失败。

### 5.1 当前关键配置

| 类别 | 关键变量 | 源码默认 | 说明 |
|---|---|---:|---|
| 文件大小 | `RAG_INGEST_MAX_FILE_BYTES` | 50 MiB | 普通文件上限 |
| PDF 大小 | `RAG_INGEST_MAX_PDF_BYTES` | 50 MiB | PDF 上限 |
| PDF 页数 | `RAG_INGEST_MAX_PDF_PAGES` | 500 | MinerU 和本地 PDF 均在请求前校验 |
| PDF 解析器 | `RAG_PDF_PARSER` | `mineru` | `mineru` 或显式回退 `local` |
| MinerU 后端 | `RAG_MINERU_BACKEND` | `pipeline` | 当前默认结构化解析后端 |
| OCR Provider | `RAG_OCR_PROVIDER` | `none` | 仅影响本地 PDF/直接图片，不控制 MinerU 内部 OCR |
| 分块 | `RAG_CHUNK_TARGET_TOKENS` | 384 | 目标 token 窗口 |
| 分块重叠 | `RAG_CHUNK_OVERLAP_TOKENS` | 64 | 必须小于目标窗口 |
| Embedding | `RAG_EMBEDDING_PROVIDER` | `pseudo` | `.env.example` 选择 `local` |
| 向量维度 | `RAG_EMBEDDING_DIM` | 1024 | 必须与模型和 Qdrant collection 一致 |
| 融合 | `RAG_SEARCH_FUSION_METHOD` | `rrf` | `rrf` 或 `weighted_score` |
| Rerank | `RAG_RERANK_PROVIDER` | `none` | `none`、`local`、`api` |
| Job 租约 | `RAG_WORKER_JOB_LEASE_SECONDS` | 120 秒 | Worker 崩溃后可被恢复 |
| 心跳 | `RAG_WORKER_HEARTBEAT_SECONDS` | 30 秒 | 在 MySQL 中续租 |

`RAG_CONFLICT_TTL_HOURS` 当前有配置项，但源码中尚未用于自动过期冲突任务。Redis 客户端也已建立，但当前业务任务心跳实际存储在 MySQL，不在 Redis。

## 6. HTTP API 总表

### 6.1 健康检查

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/health/live` | 只确认进程存活，不访问外部依赖 |
| GET | `/health` | 返回依赖详情，即使降级仍返回正常 HTTP 状态 |
| GET | `/health/ready` | 必需依赖不可用时返回 HTTP 503 |

### 6.2 入库任务

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/v1/ingest/jobs` | multipart 上传，返回 202 和 `job_id` |
| GET | `/v1/ingest/jobs/{job_id}` | 查询状态、步骤、重试次数和错误 |
| POST | `/v1/ingest/jobs/{job_id}/resolve` | 选择保留新文档或冲突候选，返回 202 |

### 6.3 文档管理

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/v1/documents` | 分页，支持 `status` 与 `q` |
| GET | `/v1/documents/{id}` | 文档元数据 |
| PATCH | `/v1/documents/{id}` | 更新标题、原始文件名或 metadata |
| DELETE | `/v1/documents/{id}` | 返回 202，异步删除双索引、产物和数据库记录 |
| GET | `/v1/documents/{id}/chunks` | 按 `chunk_index` 返回分块 |
| GET | `/v1/documents/{id}/artifacts` | 列出文档版本产物 |
| GET | `/v1/documents/{id}/artifacts/{filename}` | 下载顶层单个产物文件 |

Artifact 下载接口会拒绝空文件名、`.`、`..` 和路径穿越；OCR 图片有独立接口并额外校验必须位于对应文档的 `/ocr/` 前缀下。

### 6.4 检索

| 方法 | 路径 | 行为 |
|---|---|---|
| POST | `/v1/search` | 向量/BM25 召回、融合、可选 Rerank |

请求可独立启用或关闭 vector、keyword 和 rerank，并覆盖 top-k、融合方式、权重和过滤条件。向量与关键词不能同时关闭。

### 6.5 OCR 人工复核

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/v1/documents/{id}/ocr-regions` | 列出区域，可按状态过滤 |
| GET | `/v1/ocr-regions/{id}` | 区域详情 |
| GET | `/v1/ocr-regions/{id}/image` | 返回区域 PNG |
| POST | `/v1/ocr-regions/{id}/review` | `approve` 或 `correct` |

目前没有鉴权中间件、用户/租户隔离或 OCR 复核前端页面，这些 API 应放在可信网络边界或由上游网关保护。

## 7. 入库为什么是异步任务

解析、OCR、Embedding 和双索引写入可能持续数秒到数分钟。API 不在请求线程中直接完成这些工作，而是只完成可靠受理：

```text
Client          FastAPI          BlobStore        MySQL         RabbitMQ       Worker
  │                 │                │                │               │             │
  │ POST /jobs      │                │                │               │             │
  ├────────────────►│                │                │               │             │
  │                 │ 保存 staging  │                │               │             │
  │                 ├───────────────►│                │               │             │
  │                 │ 同一事务写 job + outbox         │               │             │
  │                 ├────────────────────────────────►│               │             │
  │ 202 queued      │                │                │               │             │
  │◄────────────────┤                │                │               │             │
  │                 │                │    Outbox relay claim/publish  │             │
  │                 │                │                ├──────────────►│             │
  │                 │                │                │  rag.ingest   │             │
  │                 │                │                │               ├────────────►│
  │                 │                │                │◄──────────────┤ claim + 租约 │
  │                 │                │                │               │ IngestPipeline
  │                 │                │                │◄────────────────────────────┤
  │                 │                │                │ succeeded / failed / conflict│
```

`JobStore.create_ingest_command()` 将 `ingest_jobs` 和 `outbox_events` 放在同一 MySQL 事务里。这样 RabbitMQ 暂时不可用时命令不会丢失，Outbox relay 恢复后仍能发布。

`RAG_WORKER_EAGER=true` 会在 API 进程内直接执行命令，只用于测试；生产应保持 `false`。

## 8. Worker、Outbox 和 RabbitMQ 可靠性

`python -m app.workers.main` 同时运行：

1. `run_outbox_publisher()`：MySQL Outbox → RabbitMQ。
2. `run_consumers()`：消费业务命令。
3. `run_recovery_loop()`：恢复过期租约和孤立半成品。

### 8.1 命令类型

| 领域命令 | 路由键/队列 | 用途 |
|---|---|---|
| `document.ingest` | `rag.ingest` | 执行文件入库 |
| `document.resolve` | `rag.resolve` | 解决文件名/来源冲突 |
| `document.cleanup` | `rag.cleanup` | 删除、回滚、替换旧文档 |
| 无 | `rag.dead` | 无效消息或重试耗尽后的死信 |

命令信封带 `event_id`、`event_type`、`aggregate_id`、`payload` 和固定 `schema_version=1`。

### 8.2 两级重试

项目存在两层不同的重试计数：

- Job attempt：业务任务被 Worker 成功 claim 的次数，默认最多 3 次。
- RabbitMQ `x-retry-count`：单条消息处理失败后的延迟投递次数，默认最多 5 次。

业务错误如果被 Pipeline 判断可重试，任务进入 `ingest_retrying` 或 `resolve_retrying`，handler 再抛出 `RetryableCommandError`，消息进入 TTL retry queue。默认延迟档位是 10 秒、60 秒、300 秒。

Outbox 自身发布失败也有独立的 `attempt/max_attempts`，使用最长 60 秒的指数退避，默认最多 20 次。

### 8.3 Job 租约与 fencing

Worker claim 时会：

- 对任务行加锁。
- `attempt += 1`。
- 生成新的 `lease_token` 和 `lease_owner`。
- 将状态设为 `running`。
- 写入 `heartbeat_at` 和 `lease_expires_at`。

执行期间心跳协程定期续租。所有关键任务状态写入都可以携带 `lease_token`；旧 Worker 丢失租约后无法把文档晚发布为 `ready`。删除文档也会撤销关联运行任务的租约，从而阻止并发删除与迟到发布竞争。

恢复循环会重新投递过期的 `running` 任务；超过最大 attempt 时标记 `MAX_ATTEMPTS_EXCEEDED`，并为遗留半成品安排 rollback。

## 9. 入库 Pipeline 完整步骤

主入口是 `IngestPipeline.run(job_id)`。正常路径如下：

```text
recover
  -> validate
  -> extract
  -> dedup
  -> conflict_check
  -> commit_artifacts
  -> chunk
  -> embed
  -> index                 # Qdrant
  -> opensearch_index
  -> publish
```

### 9.1 Recover

如果重试任务已经关联文档：

- 文档已经 `ready`：任务直接收敛为 `succeeded`。
- 文档为 `staging/indexing/failed`：先补偿清理外部索引、chunks 和 artifacts，再删除旧文档行并从原 staging 文件重跑。

文档 ID 使用 UUIDv5，由 job ID 确定性生成；chunk ID 也使用 UUIDv5，由 document ID 和 chunk index 确定性生成。这使重复执行和索引回填保持稳定 ID。

### 9.2 Validate

Pipeline 读取 staging 文件，并先用普通文件/PDF 上限的较大值做总大小检查。具体抽取器还会按文件类型再次校验。

### 9.3 Extract

`FileExtractor` 根据魔数、扩展名和请求 MIME 选择抽取器，输出统一的 `ExtractedDocument`：

```text
text            最终待分块文本
content_hash    原文件 SHA-256
source_uri      当前上传文件使用 upload://{sha256}
mime            解析后的 MIME
raw_bytes       原始字节
raw_filename    产物包中的规范文件名
metadata        页码、解析器、OCR 等附加信息
```

### 9.4 Dedup

精确去重条件为 `source_type + source_uri + content_hash`，且已有文档必须是 `ready`。命中后任务进入 `deduplicated`，复用已有 document ID，不重复生成分块和索引。

因为上传文件的 `source_uri` 当前由内容哈希生成，所以相同内容天然具有相同 URI；原始文件名不是精确去重键。

### 9.5 Conflict check

以下 ready 文档会成为冲突候选：

- 原始文件名相同，但内容哈希不同。
- source URI 相同，但内容哈希不同。

发现冲突后只创建 `staging` 文档行，不提交 artifacts 和索引：

```text
job.status = conflict
job.pending_document_id = 新文档
job.conflict_candidates = 旧文档 ID 列表
```

原始 staging 文件会保留，等待用户调用 resolve API。

### 9.6 Commit artifacts

无冲突时先创建 `indexing` 文档，然后提交版本化产物包。默认 `doc_version=1`。

```text
artifacts/{document_id}/v1/
  raw.pdf / raw.docx / raw.txt / ...
  extracted.txt
  meta.json
  ocr/base.txt            # 仅可复核 OCR 路径
  ocr/{region_id}.png     # 仅可复核 OCR 路径
```

Docker 全栈使用 MinIO；关闭 `RAG_MINIO_ENABLED` 时使用 `data/storage` 本地目录。两种实现都提供 staging、commit、read、list、delete-prefix 和 path traversal 防护。

### 9.7 Chunk、Embed、Index、Publish

文本先按 token 分块，再批量生成向量。发布写入顺序为：

```text
Qdrant upsert
  -> MySQL chunks insert
  -> OpenSearch bulk index
  -> MySQL 中 document=ready 与 job=succeeded 原子发布
  -> 删除 job staging
```

OpenSearch 是发布必需步骤，不是可选的旁路。任一索引失败都会触发 Saga 补偿，文档不会处于 `ready`。

## 10. 文件类型路由

`_guess_mime()` 优先识别 PDF、PNG、JPEG、GIF、BMP、WebP 魔数，也会探测 JSON 和 HTML；然后回退扩展名与 Python `mimetypes`。如果推断类型受支持，则优先于客户端传来的 MIME，降低伪造 MIME 带来的误路由。

| 文件 | 当前抽取器 | 主要行为 |
|---|---|---|
| PDF | 默认 `MineruPdfExtractor` | MinerU pipeline，扫描件可自动 OCR |
| PDF | `RAG_PDF_PARSER=local` 时 `PdfExtractor` | PyMuPDF 文本层 + 图片区域 OCR |
| DOCX | `MineruDocxExtractor` | 始终走 MinerU |
| TXT/LOG/TEXT | `PlainTextExtractor` | UTF-8 BOM、UTF-8、GBK、latin-1 尝试解码 |
| Markdown | `MarkdownExtractor` | 正文保留，YAML front matter 原文放 metadata |
| CSV | `CsvExtractor` | 每行转成 `cell | cell` 文本 |
| JSON | `JsonExtractor` | pretty-print，超长按配置截断 |
| HTML/XHTML | `HtmlExtractor` | 跳过 script/style/noscript/svg，抽取可见文本 |
| 常见图片 | `ImageExtractor` | 整图 OCR，必须启用 RAG OCR Provider |

不支持旧式 `.doc`、PPTX、XLSX、压缩包、音视频。虽然 MinerU API 本身可接受更多格式，RAG 的 MIME router 当前并未开放这些类型。

## 11. 当前默认 PDF 与扫描件识别逻辑

这是当前代码最需要明确的一部分。

### 11.1 默认：MinerU PDF

当 `RAG_PDF_PARSER=mineru` 时，PDF 路由到 `MineruPdfExtractor`：

1. RAG 先验证 `%PDF-`、文件大小、加密状态和页数。
2. 以 multipart 调用 `{RAG_MINERU_BASE_URL}/file_parse`。
3. 显式发送 `backend=pipeline`、返回 Markdown 和 content list。
4. 当前 RAG 请求没有显式发送 `parse_method`，因此使用 MinerU 3.4.4 服务端默认 `auto`。
5. `auto` 会在文本型 PDF 与扫描型 PDF 之间选择解析方式，扫描件由 MinerU 内部 OCR。
6. RAG 优先读取 content list，并根据 `page_idx` 重建 `--- Page N ---` 标记；没有结构化 content list 时回退 Markdown。

MinerU 请求异常的分类：

- 连接失败、超时、HTTP 429、HTTP 5xx：`MINERU_UNAVAILABLE`，可进入任务重试。
- 普通 HTTP 4xx、无有效 Markdown/内容、非法 JSON：`MINERU_PARSE_FAILED`，默认不可重试。

### 11.2 MinerU OCR 与 RAG OCR Provider 是两套机制

`RAG_OCR_PROVIDER=none` 不会关闭 MinerU 对扫描 PDF 的识别。它只控制下面两类路径：

- `RAG_PDF_PARSER=local` 的 PDF 图片区域 OCR。
- 直接上传 PNG/JPEG 等图片时的整图 OCR。

默认 MinerU 路径不会产生 `ocr_regions`、裁剪 PNG 或可人工逐区复核的数据。MinerU 已识别的文字直接作为文档正文进入分块和索引。

如果业务要求“逐区域查看原图、审批、纠正后重建索引”，应使用本地 PDF/OCR 路径，或后续为 MinerU content list 增加区域映射。

### 11.3 显式回退：本地 PDF + 区域 OCR

当 `RAG_PDF_PARSER=local` 时：

1. PyMuPDF 逐页抽取文本层。
2. `get_image_info()` 获取页面内图片 bbox。
3. 只渲染 bbox 裁剪区域，不默认渲染整页。
4. 按 DPI、最小边长、最大像素、最大字节数和每页/每文档区域数限制 OCR 开销。
5. 文本层形成不可变 `ocr/base.txt`。
6. OCR 区域按页码和 sequence 确定性附加到对应页面。
7. 裁剪图片和区域状态写入 artifact + `ocr_regions`。

典型扫描 PDF 的每页通常是一张覆盖全页的大图，因此仍会作为一个大图片 bbox 被识别；但该路径依赖 `RAG_OCR_PROVIDER=local|api`。

如果 OCR 单区失败且 `RAG_OCR_FAIL_OPEN=true`，区域记为 `failed`，其他页面继续。OCR 已尝试但全文仍为空时，Pipeline 先保存文档和 OCR 区域，再将任务标记 `EMPTY_CONTENT`，方便后续查看失败区域；它不会直接把这些复核证据回滚删除。

## 12. OCR Provider 与安全边界

本地/图片 OCR 的统一门面是 `OcrEngine`。

| 配置 | 实现 | 说明 |
|---|---|---|
| `none` | `NoneOcrProvider` | 明确禁用 |
| `local` | `PaddleOcrProvider` | PaddleOCR 2.x，懒加载并在线程池执行 |
| `api + openai_compatible` | `OpenAICompatibleOcrProvider` | `/chat/completions` 多模态 data URL |
| `api + bailian` | 同上别名 | 可从 `DASHSCOPE_API_KEY` 取密钥 |
| `api + custom` | `CustomHttpOcrProvider` | multipart 或 base64 JSON，受控 JSONPath |

远程 OCR URL 默认拒绝：

- 非 HTTP/HTTPS scheme。
- 回环、私网、链路本地、保留、多播和未指定 IP。
- DNS 解析后落到上述地址的主机名。
- HTTP redirect 跟随。

这是 SSRF 防护。只有明确的本地联调才应开启 `RAG_OCR_ALLOW_PRIVATE_URLS=true`。

错误消息在保存前会检查 `api_key`、`authorization`、`bearer`、`sk-`、`password` 等敏感标记并做脱敏/截断。

## 13. OCR 人工复核与重发布

OCR 区域状态包括：

```text
pending -> approved
pending/approved/failed/empty -> corrected
```

`approve` 只更新区域状态，不重建索引。`correct` 会：

1. 保存 `corrected_text`。
2. 读取不可变 `ocr/base.txt`。
3. 按 page + sequence 获取每个区域的有效文本；corrected 区域优先使用人工文本。
4. 使用 `merge_ocr_text()` 重建全文，避免把旧 OCR 结果重复追加。
5. 重新 chunk 和 embed。
6. 删除旧 Qdrant、OpenSearch 和 MySQL chunks。
7. 写入新双索引、chunks 和 `extracted.txt`。

重发布前会保存旧 chunks、重新计算旧向量并读取旧 `extracted.txt`。如果新发布失败，代码尽力恢复旧 Qdrant/OpenSearch/chunks/text，并把文档恢复为 `ready`；恢复本身失败才把文档标记 `failed`。

## 14. 文本分块

`chunk_extracted_text()` 使用 `Qwen/Qwen3-Embedding-0.6B` tokenizer 计算真实 token 数，并使用 LangChain `RecursiveCharacterTextSplitter`。

默认参数：

```text
target = 384 tokens
overlap = 64 tokens
separators = 段落 -> 换行 -> 。！？；， -> 空格 -> 任意字符
```

如果正文包含 `--- Page N ---` 标记，会先按页拆开，再在单页内部切分，所以 chunk 不跨页。每个 chunk metadata 保存：

```text
page_no
page_span
chunk_tokenizer_model
chunk_target_tokens
chunk_overlap_tokens
embedding_provider
embedding_download_source  # 仅本地 embedding
```

Tokenizer 懒加载并缓存。Hugging Face 模式直接加载模型名；ModelScope 模式先下载快照。本地 tokenizer 无法加载时会抛出 `CHUNKING_FAILED`，而不是退化为字符估算。

## 15. Embedding

`EmbeddingClient` 支持三种模式：

### 15.1 Pseudo

对输入 SHA-256 派生确定性向量并归一化，只用于测试和轻量开发。它不具有真实语义能力。

### 15.2 Local

懒加载 `SentenceTransformer`，可从 Hugging Face 或 ModelScope 获取模型。查询向量会按配置加上：

```text
Instruct: {embedding_query_instruction}
Query: {query}
```

文档分块不加查询指令。编码在线程池中进行，可配置 batch、device 和 normalize。

### 15.3 API

调用 OpenAI-compatible `/embeddings`：

- 按 `embedding_batch_size` 分批。
- 保持 provider 返回的 `index` 顺序。
- 如果 provider 的 HTTP 400 明确说明 batch 上限，会自动缩小批次后重试。
- 网络错误、429、5xx 标记为 retryable。
- 普通 4xx、非法响应、数量不一致通常不可重试。
- 汇总 provider 返回的 prompt/total token usage，仅记录日志，不平均分摊给 chunks。

任何模式都必须输出 `RAG_EMBEDDING_DIM` 指定的维度；Embedding 客户端和 QdrantIndexer 均会二次校验。

## 16. Qdrant 与 OpenSearch 发布

### 16.1 Qdrant

collection 名为 `{RAG_QDRANT_COLLECTION_PREFIX}chunks`，默认 `rag_chunks`，距离函数为 cosine。首次使用会创建以下 payload indexes：

```text
document_id, source_uri, original_filename, chunk_index, page_no
```

point ID 等于稳定 chunk UUID，payload 同时保存 chunk text、来源、页码、metadata 和 embedding 配置。旧 point 如果缺少 `chunk_text`，VectorRetriever 会从 MySQL chunks 回填结果文本。

### 16.2 OpenSearch

index 名为 `{RAG_OPENSEARCH_INDEX_PREFIX}chunks`，默认 `rag_chunks`。正文 `text` 使用 standard analyzer，来源字段为 keyword，metadata 字符串通过 dynamic template 映射成 keyword，方便精确过滤。

应用启动时可从 MySQL ready 文档回填 OpenSearch。搜索读路径只负责确保 index 存在，不同步执行全量 backfill，避免一次搜索被历史回填阻塞。

### 16.3 MySQL 为什么仍保存 chunks

MySQL chunks 是分块文本和发布元数据的权威副本，用于：

- 文档检查 API。
- OpenSearch 回填。
- Qdrant 旧 payload 缺文本时兜底。
- 索引重建和重新分块。
- 删除/回滚时查找 point IDs。

## 17. Saga 补偿、删除与替换

`Compensator` 将外部副作用清理拆成独立尝试：

1. Qdrant 按 `document_id` payload 删除，并补充按已知 point IDs 删除。
2. OpenSearch 按 document ID 删除。
3. 删除 artifacts 前缀。
4. 删除 MySQL chunks。
5. 根据动作更新或删除文档状态。

Qdrant 与 OpenSearch 会分别尝试，避免一个后端故障阻止另一个后端清理。

### 17.1 回滚发布失败

文档进入 `failed`。如果外部清理不完整，状态本身就是持久化恢复依据；Worker 启动和恢复循环会再次生成 rollback 命令。

### 17.2 删除文档

API 可删除 `ready`、`failed`、`deleting`、`superseded` 文档；`staging/indexing/superseeding` 会返回 409。

删除请求在同一事务中：

- 将文档改为 `deleting`。
- 取消关联的可恢复任务并撤销租约。
- 写入 `document.cleanup(action=delete)` Outbox。

双索引清理成功后才删除 artifacts、chunks、job 引用和 document 行。

### 17.3 冲突解决

保留新文档：

1. 从 staging 重新 extract。
2. 发布 pending 文档的 artifacts/chunks/双索引。
3. 旧候选进入 `superseeding` 并删除索引和产物。
4. 某个旧候选清理失败时记录 pending cleanup，但不阻止新文档发布。
5. 新文档与任务原子进入 `ready/succeeded`。

保留旧文档：

1. rollback pending 文档。
2. 任务进入 `discarded`，document ID 指向被保留的旧文档。
3. 删除 staging。

检索结果最后还会通过 MySQL `ready_ids()` 过滤。因此即使外部索引中暂时残留 deleting、failed 或 superseeding 文档，也不会返回给调用方。

## 18. 文档与任务状态机

### 18.1 文档状态

```text
新文档 ──正常发布──────────────► indexing ──双索引成功──► ready
   │                                │                     ├──用户删除──► deleting ──清理完成──► 已删除
   │                                └──发布失败──────────► failed ─────┘
   │
   └──发生冲突──► staging ──保留新文档──► indexing

ready ──被新版本替换──► superseeding ──清理完成──► superseded
```

### 18.2 任务状态

```text
queued
  -> running
      -> succeeded
      -> deduplicated
      -> conflict
      -> ingest_retrying -> running
      -> failed
      -> cancelled

conflict
  -> resolving
      -> running
          -> succeeded       # 保留新文档
          -> discarded       # 保留旧文档
          -> resolve_retrying -> running
          -> failed
```

`current_step` 比 `status` 更细，前端用它显示 validate/extract/chunk/embed/index 等实时阶段。每次阶段开始都会追加到 `step_logs`。

## 19. 混合检索完整逻辑

```text
POST /v1/search
      ├──► 生成 query embedding ──► Qdrant cosine top-k ──┐
      │                                                   │
      └──────────────────────────► OpenSearch BM25 top-k ─┤
                                                          ▼
                                              RRF / weighted_score
                                                          │
                                                          ▼
                                               MySQL ready 状态过滤
                                                          │
                                                          ▼
                                                    可选 Rerank
                                                          │
                                                          ▼
                                                格式化结果和降级信息
```

向量和关键词召回使用 `asyncio.gather(..., return_exceptions=True)` 并行执行。

### 19.1 后端降级规则

- 两个启用的后端都失败：HTTP 503。
- 一个失败，另一个返回可靠结果：返回 `search_status=degraded` 和单引擎模式。
- 一个失败，另一个虽然健康但结果也为空：视为没有可靠结果，HTTP 503。
- 两个后端都健康且确实无命中：正常返回空数组。
- Rerank 失败：保留融合顺序并标记 `degraded_components=["rerank"]`。

`effective_mode` 为 `hybrid`、`vector_only` 或 `keyword_only`，反映实际可用的召回后端，不只是请求开关。

### 19.2 RRF

默认融合公式：

```text
score(chunk) = Σ 1 / (RAG_SEARCH_RRF_K + rank + 1)
```

默认 `RRF_K=60`。它只依赖名次，对 cosine 和 BM25 的不同分数尺度更稳健。

### 19.3 Weighted score

两侧原始分数先各自 min-max 到 `[0,1]`，再相加：

```text
score = normalized_vector * vector_weight
      + normalized_keyword * keyword_weight
```

一侧所有分数相等时统一归一化为 1，避免除零。当前 API 允许权重和不等于 1；后端也没有禁止两个权重同时为 0，前端会主动拦截该情况。

### 19.4 统一过滤契约

支持以下精确过滤：

```text
document_id
source_uri
original_filename
chunk_index
page_no
metadata.{受控键}
```

Pydantic 禁止未知字段；metadata key 只能包含字母、数字、下划线和连字符。相同逻辑会转换为 Qdrant `must` 条件、OpenSearch `term` 条件或 pseudo retriever 内存判断。

### 19.5 Rerank

- `none`：按融合顺序直通。
- `local`：使用 `FlagEmbedding.FlagReranker`，在线程池中计算 BGE score。
- `api`：调用 `{base_url}/reranks`，严格校验索引、数量和 relevance score。

只对融合结果前 `RAG_RERANK_TOP_K` 个候选重排，然后截取最终 top-k。百炼误填 embedding 的 `/compatible-mode/v1` 地址时，代码会针对 `.maas.aliyuncs.com` 自动修正为 rerank 的 `/compatible-api/v1`。

## 20. 数据模型

### 20.1 documents

保存来源、哈希、文件名、版本、状态、artifact 路径和业务 metadata。`status` 是文档是否可检索的权威字段。

### 20.2 chunks

保存稳定 ID、正文、token 数、页码、Embedding 配置、Qdrant point ID 和 metadata。MySQL 初始化 SQL 为 document ID 配置了级联删除外键。

### 20.3 ingest_jobs

保存任务来源、选项、状态、步骤、关联/待定文档、冲突候选、attempt、错误、步骤日志和 Worker 租约。

### 20.4 outbox_events

保存尚未可靠发布或已经发布的领域命令，包括发布租约、退避时间、错误和最大尝试次数。

### 20.5 ocr_regions

保存页码、bbox、crop 路径、机器文本、人工文本、状态、provider、confidence、错误和 sequence metadata。

## 21. 健康检查语义

`/health/live` 不访问依赖。

`RAG_MODE=ingest` 时，readiness 必需项是：

- MySQL。
- MinIO 或本地存储二选一。
- 真实启用时的 Qdrant。
- 真实启用时的 OpenSearch。
- MinerU。

即使 PDF 显式选择 `local`，MinerU 仍是必需项，因为 DOCX 始终宣传为支持并始终路由到 MinerU。

RabbitMQ 会在 ingest health 中报告，但不阻止 API readiness。原因是 API 可以把任务可靠写入 Outbox，RabbitMQ 短暂故障不会导致受理数据丢失。

当前 ingest 模式不会报告 Redis；Compose 仍会启动 Redis，完整 `rag_mode` 才遍历全部 `_CHECKS`。

## 22. 前端控制台

前端是原生 HTML/CSS/JavaScript，没有 React/Vue 构建步骤。

主要功能：

- 拖放上传和 50 MB 前端校验。
- 使用 localStorage 保存最近 30 个 job ID，并轮询任务状态。
- 文档分页、筛选、改标题和异步删除。
- 文档详情、chunks 和 artifacts 数量检查。
- 混合检索开关、融合方式、权重和来源 URI 过滤。
- 健康检查与依赖状态展示。

前端不是权威状态存储；清除“最近入库任务”只清除浏览器 localStorage，不会删除服务端任务或文档。

当前前端没有以下功能：

- 冲突候选选择界面。
- OCR 区域复核界面。
- Artifact 文件下载入口。
- 用户登录、权限或租户管理。

## 23. Docker 构建与运行

### 23.1 RAG 镜像

`Dockerfile` 基于 Python 3.11 slim，使用 uv 锁定安装依赖，并安装 `local-embedding` extra。API 和 Worker 复用同一个镜像，Worker 仅覆盖 command。

Hugging Face 模型目录挂载到命名卷 `/models/huggingface`，避免每次重建重新下载 Embedding 模型。

### 23.2 MinerU 镜像

`Dockerfile.mineru` 当前默认：

```text
python:3.11-slim
mineru[core]==3.4.4
MINERU_MODEL_TYPE=pipeline
MINERU_MODEL_SOURCE=modelscope
```

它安装字体、GLib、OpenGL/X11 运行库，下载 pipeline 模型，然后将 BuildKit 下载缓存复制到 `/opt/mineru-models` 并修改 `/root/mineru.json`。这样最终镜像实际包含离线模型，而不是只在构建缓存中短暂存在。

Compose 为 MinerU 预约指定 NVIDIA GPU，默认最大并发 1，使用 `/health` 探针。`rag-service` 和 `rag-worker` 必须等 MinerU 健康后启动。

### 23.3 开发环境警告

- Compose 中 OpenSearch 关闭安全插件，只适合开发。
- 默认 MinIO/MySQL/RabbitMQ 凭据只适合本地环境。
- MinerU API 监听 `0.0.0.0`，应通过防火墙或反向代理限制外部访问。
- 当前 RAG API 本身没有认证，不应直接暴露到公网。

## 24. 索引维护工具

### 24.1 OpenSearch backfill

应用启动时运行 `backfill_ready_documents()`，只读取 MySQL ready 文档的 active chunks，以稳定 chunk ID 幂等覆盖 OpenSearch。

### 24.2 重建双索引

```powershell
python -m app.core.indexing.rebuild_search_indexes
```

流程：先为全部 ready chunks 生成向量，全部准备成功后删除并重建 Qdrant collection 和 OpenSearch index，再同步 chunks 中的 Embedding 配置。

### 24.3 重新分块并重建

```powershell
python -m app.core.indexing.rechunk_search_indexes
```

它从 artifacts 的 `extracted.txt` 重新读取正文，在修改存储之前为全部文档完成新分块和向量准备，然后整体替换 Qdrant、OpenSearch 和 MySQL chunks。修改 tokenizer、窗口或 overlap 后应使用这个命令，而不是只重建索引。

这两个工具都会替换整个业务索引，应该在停止 API/Worker 写入的维护窗口运行。

## 25. 网络安全检索评测

`evaluation/cybersecurity` 提供固定快照语料、开发集、盲测问题、gold 标注和结果报告。

评测脚本直接调用真实 `/v1/search`，根据结果中的文件名和页码判断证据命中，并计算：

- Hit@k。
- Recall@k。
- MRR@k。
- nDCG@k。
- 不可回答问题的误召回情况。
- 请求失败、超时和无效 JSON 统计。

可以通过关闭 vector、keyword 或 rerank 做消融实验。第一阶段评测的是检索，不是 LLM 答案质量。

## 26. 自动化测试覆盖

当前测试覆盖的主要边界包括：

- PDF、DOCX、TXT 的端到端入库。
- MinerU 路由、页码恢复、超时和服务端错误。
- 扫描 PDF/图片 OCR、fail-open、Provider 协议和 SSRF 防护。
- OCR approve/correct 及重建、失败回滚。
- 中文 token 分块、overlap 和不跨页。
- Embedding pseudo/local/API、批次缩小、usage、维度校验。
- Qdrant/OpenSearch 索引和删除一致性。
- RRF、加权归一化、过滤、Rerank 和部分降级。
- 文档 CRUD、冲突保留新/旧、精确去重。
- Outbox 原子提交、租约 fencing、Worker 恢复、死信前重试。
- Saga 发布失败回滚、孤立文档清理和删除幂等性。
- 网络安全评测脚本的请求失败容错。

常用验证命令：

```powershell
uv run ruff check app tests evaluation
uv run python -m pytest -q
uv lock --check
docker compose config --quiet
git diff --check
```

## 27. 当前已知限制和文档差异

以下不是本次推测，而是当前源码中可以直接观察到的边界：

1. `frontend/index.html` 的上传提示仍写着“PDF 默认本地解析”，实际默认已经是 MinerU。
2. 旧文档 `docs/pdf-ingest-embedding-flow.md` 部分章节仍描述“PDF 只有本地文本层、不自动 OCR”，已落后于当前源码。
3. MinerU PDF 不生成 `ocr_regions`，所以能识别扫描件，但不能使用现有逐区域人工复核 API。
4. 直接上传图片仍要求 `RAG_OCR_PROVIDER=local|api`；默认 MinerU PDF OCR 不会自动接管图片路由。
5. 前端没有冲突解决和 OCR 复核 UI，需要直接调用 API。
6. `RAG_CONFLICT_TTL_HOURS` 暂未进入冲突过期清理逻辑。
7. Redis 当前主要是预留客户端/健康能力，未承载任务租约、缓存或限流业务。
8. 没有认证、授权、租户隔离、审计日志和 API rate limit。
9. OpenSearch 使用 standard analyzer，没有专门中文分词插件；中文效果主要依赖原始字符/词边界和混合向量召回。
10. 数据库采用初始化 SQL + 启动时补建的轻量方式，尚未引入 Alembic 等正式 migration 工具。
11. `.env.example`、Compose 显式 environment 和宿主机直跑配置具有不同覆盖层，排障时必须查看最终 `docker compose config`，不能只看 `.env`。
12. 本地 OCR 全文为空时，Pipeline 会先保留 failed 文档和 OCR 区域；但 Worker 启动会把所有 failed 文档重新加入 rollback 清理，因此这些裁剪图片不能保证跨 Worker 重启长期保留。
13. `ocr_regions` 当前没有数据库外键级联，文档硬删除和 supersede 也没有显式删除 OCR 区域行，可能留下失去 artifact 图片的 OCR 元数据记录。

## 28. 一次典型请求的端到端示例

以扫描版 `report.pdf` 为例：

1. 浏览器向 `/v1/ingest/jobs` 上传文件。
2. API 把字节写入 MinIO `staging/jobs/{job_id}/upload`。
3. MySQL 原子创建 queued job 和 `document.ingest` Outbox。
4. Outbox relay 发布到 `rag.ingest`。
5. Worker claim job 并创建租约。
6. FileExtractor 根据 `%PDF-` 和 `RAG_PDF_PARSER=mineru` 选择 MinerU。
7. RAG 校验加密、大小和页数，调用 MinerU `pipeline + auto`。
8. MinerU 对扫描页执行 OCR，返回 content list/Markdown。
9. RAG 重建页码标记，计算 SHA-256，并检查去重/文件名冲突。
10. 无冲突时写入 raw PDF、`extracted.txt` 和 `meta.json`。
11. Qwen tokenizer 在每页内生成 384/64 token chunks。
12. Qwen Embedding 或配置的 provider 生成 1024 维向量。
13. Qdrant 保存向量和来源 payload。
14. MySQL 保存 chunks。
15. OpenSearch 保存 BM25 文本索引。
16. MySQL 原子把 document 设为 ready、job 设为 succeeded。
17. 删除 staging 文件。
18. 后续 search 并行查 Qdrant/OpenSearch，融合、过滤非 ready 文档并可选 Rerank。
19. 返回 chunk 文本、综合/分项分数、文件名、页码和 document ID。

这条链路的关键保证是：只有 artifacts、chunks、Qdrant 和 OpenSearch 全部发布成功，文档才对检索可见；中间失败会进入可恢复状态并执行补偿。
