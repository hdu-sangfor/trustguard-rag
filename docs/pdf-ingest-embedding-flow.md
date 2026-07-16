# PDF 入库、切片、Embedding 与双索引发布流程

本文记录当前 `trustguard-rag` 中 PDF 文件从 HTTP 上传到 MySQL、Qdrant 和 OpenSearch 发布完成的实际调用链。

适用范围：

- 文件来源：`source_type=file`
- 文件类型：PDF
- 入库接口：`POST /v1/ingest/jobs`
- 向量提供方：`pseudo`、本地 Sentence Transformers 或 OpenAI-compatible Embedding API
- 向量索引：Qdrant
- 关键词索引：OpenSearch；mock 模式下为进程内模拟实现
- 异步执行：Transactional Outbox + RabbitMQ Worker

## 1. 总体调用链

```text
POST /v1/ingest/jobs
  -> app.api.ingest.create_ingest_job()
      -> BlobStore.put_job_upload(job_id, data)
      -> JobStore.create_ingest_command()
          -> 同一 MySQL 事务写 ingest_jobs + outbox_events(document.ingest)
      -> dispatch_eager(event)  # 仅 RAG_WORKER_EAGER=true 的测试模式执行
  <- 202 {"job_id": "...", "status": "queued"}

独立进程：python -m app.workers.main
  -> run_outbox_publisher()
      -> OutboxStore.claim_batch()
      -> RabbitMQ rag.commands exchange
      -> rag.ingest queue
  -> run_consumers()
      -> handlers.dispatch_command()
      -> _handle_ingest()
      -> JobStore.claim()
      -> IngestPipeline.run(job_id)
          -> _load_upload()
          -> FileExtractor.extract()
              -> PdfExtractor.extract()
          -> DocumentStore.find_by_source()       # 精确去重
          -> _detect_conflicts()                  # 文件名/来源冲突
          -> DocumentStore.create(indexing)
          -> _commit_artifacts()
          -> chunk_extracted_text()
          -> EmbeddingClient.embed_texts()
          -> QdrantIndexer.upsert_chunks()
          -> ChunkStore.create_many()
          -> OpenSearchIndexer.index_chunks()
          -> DocumentStore.update_status(ready)
          -> JobStore.finish(succeeded)
          -> BlobStore.delete_job_staging()
```

当前生产路径不使用 FastAPI `BackgroundTasks`，API 与 Worker 是两个独立执行单元。Docker Compose 中分别是 `rag-service` 和 `rag-worker`。

## 2. 上传入口与事务 Outbox

文件：

```text
app/api/ingest.py
app/stores/job_store.py
app/stores/outbox_store.py
```

接口：

```http
POST /v1/ingest/jobs
```

PowerShell 示例：

```powershell
curl.exe -X POST http://localhost:18200/v1/ingest/jobs `
  -F "source_type=file" `
  -F "file=@report.pdf"
```

`create_ingest_job()` 的执行顺序：

1. 只接受 `source_type == "file"`。
2. 读取上传字节、原始文件名和请求 MIME。
3. 预先生成 `job_id`，把原文件写入 staging：

   ```python
   bs.put_job_upload(job_id, data)
   ```

4. 调用 `JobStore.create_ingest_command()`，在同一个 MySQL 事务内创建：

   - `ingest_jobs`：初始状态 `queued`
   - `outbox_events`：事件类型 `document.ingest`，payload 为 `{"job_id": job_id}`

5. 如果 MySQL 事务失败，删除刚写入的 staging。
6. 调用 `dispatch_eager(event)`；默认 `RAG_WORKER_EAGER=false` 时它不会执行命令。
7. 返回 HTTP `202 Accepted`：

   ```json
   {
     "job_id": "...",
     "status": "queued"
   }
   ```

这种顺序保证业务任务和待发布命令一起提交。RabbitMQ 暂时不可用时，任务仍保留在 Outbox 中，API 不会因消息未立即投递而丢任务。

> `RAG_WORKER_EAGER=true` 仅用于确定性自动化测试：事务提交后在 API 进程内直接执行命令，并在成功后把 Outbox 标为 `published`。生产环境应保持关闭。

## 3. Outbox 发布和 RabbitMQ 消费

相关文件：

```text
app/workers/main.py
app/workers/publisher.py
app/workers/consumer.py
app/workers/handlers.py
app/workers/messages.py
app/stores/rabbitmq.py
```

Worker 启动命令：

```powershell
python -m app.workers.main
```

`app.workers.main` 同时运行两个常驻协程：

- `run_outbox_publisher()`：轮询 MySQL Outbox，并把持久化命令发布到 RabbitMQ。
- `run_consumers()`：消费 RabbitMQ 命令并调用幂等 handler。

### 3.1 Outbox relay

默认配置：

```env
RAG_WORKER_OUTBOX_POLL_SECONDS=1
RAG_WORKER_OUTBOX_BATCH_SIZE=50
RAG_WORKER_OUTBOX_LEASE_SECONDS=60
```

Relay 会租用 `pending` 事件以及租约过期的 `publishing` 事件，发布持久化消息并等待 broker confirm。确认后事件变为 `published`；发布失败则回到 `pending` 并按最多 60 秒的指数退避重试。Outbox 行默认最多尝试 20 次，耗尽后变为 `dead`。

### 3.2 命令与队列

```text
document.ingest  -> rag.ingest
document.resolve -> rag.resolve
document.cleanup -> rag.cleanup
失败死信         -> rag.dead
```

消息 envelope 包含：

```json
{
  "event_id": "...",
  "event_type": "document.ingest",
  "aggregate_id": "job_id",
  "payload": {"job_id": "..."},
  "schema_version": 1
}
```

消费者使用手动 ack，默认 `prefetch_count=1`。命令失败时不是原地 sleep，而是发布到带 TTL 的 retry queue；TTL 到期后由 dead-letter 路由回业务队列。

默认配置：

```env
RAG_RABBITMQ_CONSUMER_MAX_RETRIES=5
RAG_RABBITMQ_RETRY_DELAYS_MS=10000,60000,300000
RAG_WORKER_JOB_LEASE_SECONDS=1800
```

超出延迟档位后继续使用最后一档。消费者重试次数耗尽后，命令进入 `rag.dead`。

### 3.3 Job claim 与幂等

`_handle_ingest()` 只认领状态为 `queued` 或 `ingest_retrying` 的任务。`JobStore.claim()` 会：

- 对任务行加锁；
- 把任务状态改为 `running`；
- 每次成功 claim 时 `attempt += 1`；
- 允许重新认领超过 `RAG_WORKER_JOB_LEASE_SECONDS` 的陈旧 `running` 任务；
- 默认最多认领 3 次，耗尽后标记 `failed / MAX_ATTEMPTS_EXCEEDED`。

重复投递遇到终态任务会直接返回；遇到仍在有效租约内的 `running` 任务会要求 RabbitMQ 延迟重投。因此消费语义是至少一次投递，业务层通过状态和租约实现幂等保护。

## 4. Pipeline 状态与步骤

主文件：

```text
app/core/ingest/pipeline.py
```

正常入库过程中记录的 `current_step`：

```text
validate
extract
dedup
conflict_check
commit_artifacts
chunk
embed
index
opensearch_index
publish
```

每次 `mark_running()` 还会向 `step_logs` 追加一条 `status=started` 的日志，并刷新任务运行时间。任务查询响应同时返回：

```text
status, current_step, document_id, pending_document_id,
conflict_candidates, error_code, error_message,
attempt, max_attempts, step_logs, created_at, started_at, finished_at
```

如果重试前 `job.document_id` 指向一个 `failed` 文档，Pipeline 会先再次执行补偿清理。清理完整后删除旧文档行、清空任务的 `document_id`，再从上传原文件重新跑整条流程；清理不完整则继续等待重试。

## 5. 读取暂存文件

`IngestPipeline._load_upload()` 从 `job.options_json` 读取：

```python
original_filename = opts.get("original_filename", "upload.bin")
mime = opts.get("mime")
data = self._blobs.read_job_upload(job.id)
```

本地后端默认路径：

```text
data/storage/staging/jobs/{job_id}/upload
```

启用 MinIO 后，bucket 内对象 key：

```text
staging/jobs/{job_id}/upload
```

可重试任务会保留这个 staging 原文件；成功、去重、丢弃和不可重试失败进入终态时才删除。

## 6. MIME 路由与 PDF 抽取

入口文件：

```text
app/core/ingest/extractors/file.py
app/core/ingest/extractors/pdf.py
```

`FileExtractor` 当前只注册：

```python
MIME_ROUTER = {
    "application/pdf": PdfExtractor(),
}
```

如果上传请求提供了 MIME，优先使用请求 MIME；没有提供时，先检查 `%PDF-` 文件签名，再按文件扩展名猜测。无法路由时抛出 `UNSUPPORTED_MIME`。

`PdfExtractor.extract()` 的步骤：

1. 校验字节数不超过 `RAG_INGEST_MAX_PDF_BYTES`；Pipeline 的 `validate` 步骤也会先做一次总大小检查。
2. 校验文件头为 `%PDF-`。
3. 用 PyMuPDF 打开：`fitz.open(stream=data, filetype="pdf")`。
4. 拒绝需要密码的 PDF。
5. 校验页数不超过 `RAG_INGEST_MAX_PDF_PAGES`。
6. 逐页执行 `doc.load_page(i).get_text("text")`。
7. 记录实际含文本的页码；如果所有页面都没有文本层，抛出 `PDF_NO_TEXT_LAYER`，当前不会自动 OCR。
8. 为每页插入页码标记：

   ```text
   --- Page 1 ---
   ...
   --- Page 2 ---
   ...
   ```

9. 在 `finally` 中关闭 PyMuPDF 文档对象。

返回 `ExtractedDocument`：

```python
ExtractedDocument(
    text=full_text,
    content_hash=sha256(data).hexdigest(),
    source_uri=f"upload://{content_hash}",
    mime="application/pdf",
    raw_bytes=data,
    raw_filename="raw.pdf",
    metadata={
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "original_filename": original_filename,
        "file_size": len(data),
    },
)
```

## 7. 精确去重与冲突检测

### 7.1 精确去重

Pipeline 调用：

```python
DocumentStore.find_by_source(
    job.source_type,
    extracted.source_uri,
    extracted.content_hash,
)
```

只有找到状态为 `ready` 的完全相同文档才算去重成功：

```text
job.status = deduplicated
job.document_id = existing.id
```

随后删除 job staging 并结束，不再创建 artifact、切片、Embedding 或索引。

### 7.2 冲突检测

`IngestPipeline._detect_conflicts()` 只把已发布的 `ready` 文档作为冲突候选：

1. 原始文件名相同、内容哈希不同；
2. `source_uri` 相同、内容哈希不同。

发现冲突时创建新文档记录：

```text
document.status = staging
job.status = conflict
job.pending_document_id = 新文档 ID
job.conflict_candidates = 已有文档 ID 列表
job.error_code = FILENAME_CONFLICT
```

此时不提交 artifact，也不执行 `chunk -> embed -> index`，并保留 staging 原文件供后续解决。

## 8. 冲突解决的异步流程

接口：

```http
POST /v1/ingest/jobs/{job_id}/resolve
Content-Type: application/json

{"keep_document_id": "..."}
```

`JobStore.request_resolution()` 会在同一个事务内：

- 校验任务当前为 `conflict`；
- 校验选择的是 `pending_document_id` 或候选旧文档之一；
- 保存选择，把状态改为 `resolving`；
- 把 `attempt` 重置为 0；
- 创建 `document.resolve` Outbox 事件。

`rag.resolve` 消费者认领后有两个分支：

### 8.1 保留新上传文档

重新读取并抽取 staging PDF，然后对 pending 文档执行：

```text
indexing
  -> commit artifacts
  -> chunk
  -> embed
  -> Qdrant
  -> MySQL chunks
  -> OpenSearch
  -> ready
```

新文档发布后，对冲突候选旧文档执行 `supersede_document()`：先置为 `superseeding`，再清理 Qdrant、OpenSearch、artifact 和 chunks，最后置为 `superseded`。某个旧文档清理失败时会额外写入 `document.cleanup` Outbox 事件，主冲突任务仍可完成为 `succeeded`，并在 `step_logs` 记录待清理数量。

### 8.2 保留已有文档

对 pending 文档执行回滚；如果没有完全清理成功，则追加 `document.cleanup(action=rollback)` 事件。任务结束为：

```text
job.status = discarded
job.document_id = 选中的已有文档 ID
```

两个分支都会在终态删除 job staging。冲突解决的可重试状态为 `resolve_retrying`。

## 9. Artifact 提交

无冲突时先创建：

```text
document.status = indexing
job.document_id = document.id
```

随后调用：

```python
IngestPipeline._commit_artifacts()
  -> BlobStore.commit_bundle()
```

本地默认写入：

```text
data/storage/artifacts/{document_id}/v1/extracted.txt
data/storage/artifacts/{document_id}/v1/meta.json
data/storage/artifacts/{document_id}/v1/raw.pdf
```

MinIO 使用相同逻辑 key：

```text
artifacts/{document_id}/v1/{filename}
```

`meta.json` 包含内容哈希、MIME、来源 URI 和抽取元数据，例如：

```json
{
  "content_hash": "...",
  "mime": "application/pdf",
  "source_uri": "upload://...",
  "page_count": 10,
  "pages_with_text": [1, 2, 3],
  "original_filename": "report.pdf",
  "file_size": 123456
}
```

提交成功后，逻辑目录 `artifacts/{document_id}/v1` 写入 `document.blob_path`。提交失败统一转换成 `ARTIFACT_WRITE_FAILED`。

## 10. PDF 文本切片

文件：

```text
app/core/ingest/chunker.py
```

入口：

```python
chunk_extracted_text(extracted.text)
```

默认目标大小：

```env
RAG_CHUNK_TARGET_TOKENS=512
```

切片规则：

1. 用 `--- Page N ---` 标记拆页，因此普通 PDF chunk 不跨页。
2. 每页内部按空行拆成段落。
3. token 数是启发式估算：`max(1, len(text) // 4)`，不是模型 tokenizer 的精确结果。
4. 段落累计到目标大小；加入下一段会超限时输出当前 chunk。
5. 单个超长段落按 `target_tokens * 4` 个字符硬切。
6. 空白页不产生 chunk。

输出 `ChunkDraft`：

```python
ChunkDraft(
    text="...",
    page_no=1,
    page_span="1",
    token_count=123,
    metadata={"page_no": 1, "page_span": "1"},
)
```

当前没有 overlap、parent-child 或跨页合并结构。如果没有生成任何 chunk，Pipeline 以 `EMPTY_CONTENT` 失败。

## 11. Embedding

文件：

```text
app/core/embedding/client.py
```

调用：

```python
EmbeddingClient.embed_texts([draft.text for draft in drafts])
```

provider 的规范值为：

```text
api
local
pseudo
```

代码也接受部分别名，例如 `openai_compatible -> api`、`huggingface/modelscope -> local`、`mock/fake -> pseudo`。所有 provider 最终都必须满足：

```python
len(vector) == settings.embedding_dim
```

文档切片使用 `embed_texts()`，不会添加查询指令；只有检索侧的 `embed_query()` 会使用 `RAG_EMBEDDING_QUERY_INSTRUCTION`。

### 11.1 API 模式

```env
RAG_EMBEDDING_PROVIDER=api
RAG_EMBEDDING_MODEL=text-embedding-v4
RAG_EMBEDDING_DIM=1024
RAG_EMBEDDING_BATCH_SIZE=16
RAG_EMBEDDING_BASE_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
RAG_EMBEDDING_API_KEY=...
RAG_EMBEDDING_API_TIMEOUT_SECONDS=60
```

按 `RAG_EMBEDDING_BATCH_SIZE` 分批请求：

```http
POST {RAG_EMBEDDING_BASE_URL}/embeddings
Authorization: Bearer ...
Content-Type: application/json
```

```json
{
  "model": "text-embedding-v4",
  "input": ["chunk text 1", "chunk text 2"]
}
```

每批返回按 `index` 排序，并校验返回向量数与输入数一致。HTTP `429`、HTTP `5xx` 和网络错误标记为可重试；其他 HTTP 错误、响应结构错误、数量错误或维度错误默认不可重试。

### 11.2 local 模式

宿主机需要安装可选依赖：

```powershell
uv sync --extra local-embedding
```

示例：

```env
RAG_EMBEDDING_PROVIDER=local
RAG_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RAG_EMBEDDING_DIM=1024
RAG_EMBEDDING_BATCH_SIZE=16
RAG_EMBEDDING_NORMALIZE=true
RAG_EMBEDDING_DOWNLOAD_SOURCE=huggingface
```

也可使用：

```env
RAG_EMBEDDING_DOWNLOAD_SOURCE=modelscope
RAG_MODELSCOPE_ENDPOINT=https://www.modelscope.cn
```

模型首次使用时延迟加载，实际 `model.encode()` 在线程池中执行。

### 11.3 pseudo 模式

```env
RAG_EMBEDDING_PROVIDER=pseudo
```

该模式对文本 SHA-256 派生值生成确定性归一化伪向量，适合测试和轻量开发，不代表真实语义模型。

## 12. 组装 chunk_rows

Embedding 完成后，为每个 chunk 生成 UUID；`chunk_index` 从 0 开始：

```python
{
    "id": cid,
    "document_id": doc.id,
    "chunk_index": i,
    "text": draft.text,
    "token_count": draft.token_count,
    "page_no": draft.page_no,
    "embedding_model": settings.embedding_model,
    "embedding_dim": settings.embedding_dim,
    "qdrant_point_id": cid,
    "metadata": {
        **draft.metadata,
        "embedding_provider": normalized_provider,
        # 仅 local provider 额外记录：
        "embedding_download_source": settings.embedding_download_source,
    },
}
```

`embedding_download_source` 只在规范化 provider 为 `local` 时写入；API 和 pseudo 不写这个字段。

## 13. Qdrant 向量索引

文件：

```text
app/core/indexing/qdrant_indexer.py
```

真实索引开关：

```env
RAG_QDRANT_MOCK=false
```

`true` 时使用 `MockQdrantIndexer`，只校验 chunk/vector 数量一致，不连接 Qdrant。

collection 名称：

```text
{RAG_QDRANT_COLLECTION_PREFIX}chunks
```

默认是 `rag_chunks`。首次使用时按以下配置创建：

```python
VectorParams(
    size=settings.embedding_dim,
    distance=Distance.COSINE,
)
```

每个 point 的 ID 是 chunk UUID，payload 为：

```json
{
  "chunk_text": "原始 chunk 文本",
  "doc_id": "document UUID",
  "chunk_index": 0,
  "page_no": 1,
  "source_uri": "upload://...",
  "original_filename": "report.pdf",
  "embedding_model": "text-embedding-v4",
  "embedding_dim": 1024,
  "embedding_provider": "api"
}
```

写入前会再次校验 chunk/vector 数量以及每条向量维度。底层异常统一转成 `INDEX_FAILED`。

## 14. MySQL chunks 与 OpenSearch

发布顺序是：

```text
Qdrant upsert
  -> MySQL chunks insert
  -> OpenSearch index
```

### 14.1 MySQL chunks

`ChunkStore.create_many()` 写入：

```text
id
document_id
chunk_index
text
token_count
page_no
embedding_model
embedding_dim
qdrant_point_id
metadata_json
status = active
```

Qdrant 在 MySQL chunks 之前写入，因此如果 MySQL 插入失败，补偿器必须按 `doc_id` 删除 Qdrant points，不能只依赖数据库中已保存的 point ID。

### 14.2 OpenSearch

文件：

```text
app/core/indexing/opensearch_indexer.py
app/core/retrieval/keyword_retriever.py
```

真实 OpenSearch 开关：

```env
RAG_SEARCH_OPENSEARCH_MOCK=false
```

索引名：

```text
{RAG_OPENSEARCH_INDEX_PREFIX}chunks
```

默认是 `rag_chunks`。真实模式下先 `ensure_index()`，再 bulk 写入并 `refresh=true`。字段包括：

```text
chunk_id, document_id, chunk_index, text,
source_uri, original_filename, page_no, metadata
```

OpenSearch 是发布必需步骤。建索引或 bulk 写入失败会转换为 `INDEX_FAILED`，触发整条 Saga 回滚，文档不能进入 `ready`。

mock 模式下使用进程内 `PseudoKeywordRetriever`，不访问真实 OpenSearch；它与生产 OpenSearch 不共享持久数据。

## 15. 发布完成

Qdrant、MySQL chunks 和 OpenSearch 全部成功后：

```python
DocumentStore.update_status(doc.id, DocumentStatus.READY)
JobStore.finish(job_id, "succeeded", document_id=doc.id)
BlobStore.delete_job_staging(job_id)
```

结果：

```text
document.status = ready
job.status = succeeded
staging upload 被删除
```

查询任务：

```powershell
curl.exe http://localhost:18200/v1/ingest/jobs/{job_id}
```

查询 chunks：

```powershell
curl.exe http://localhost:18200/v1/documents/{document_id}/chunks
```

## 16. 失败、补偿与重试

文件：

```text
app/core/ingest/compensator.py
```

只要已经创建 `document_id`，Pipeline 在异常路径会调用：

```python
Compensator.rollback_document(document_id)
```

补偿动作：

1. Qdrant 同时按 `doc_id` payload 和已知 point IDs 删除向量；
2. 按 `document_id` 删除 OpenSearch 文档；
3. 删除 artifact 前缀；
4. 删除 MySQL chunks；
5. 把 document 状态改为 `failed`。

Qdrant 与 OpenSearch 会独立尝试清理，避免一个失败阻止另一个。`rollback_document()` 返回是否已全部清理成功；失败文档状态作为后续恢复依据。

### 16.1 可重试失败

以下情况进入 `ingest_retrying`：

- `INDEX_FAILED`
- `ARTIFACT_WRITE_FAILED`
- 标记为 `retryable=True` 的 `EmbeddingError`
- 未分类的意外异常

此时 staging 原文件保留。handler 抛出 `RetryableCommandError`，RabbitMQ 将命令送入延迟队列。任务默认最多 claim 3 次；最后一次仍失败时直接变为 `failed` 并删除 staging。

### 16.2 不可重试失败

其他 `IngestError`，以及不可重试的 `EmbeddingError`，会直接：

```text
job.status = failed
document.status = failed  # 已创建文档时
staging upload 被删除
```

### 16.3 Worker 启动恢复

Worker 启动时，`DocumentStore.enqueue_pending_cleanups()` 扫描：

```text
deleting     -> document.cleanup(action=delete)
superseeding -> document.cleanup(action=supersede)
failed       -> document.cleanup(action=rollback)
```

并写入新的 Outbox 事件。清理 handler 会先检查文档当前状态，旧的或重复的 cleanup 命令不会误删已经恢复为 `ready` 的文档。

## 17. 验证命令

### 17.1 启动 API、Worker 与依赖

```powershell
docker compose up -d --build
docker compose ps rag-service rag-worker rabbitmq qdrant opensearch
docker compose logs -f rag-worker
```

### 17.2 单独验证 Embedding

在 `trustguard-rag` 目录执行：

```powershell
$env:PYTHONPATH="."

@'
import asyncio
from app.core.embedding.client import EmbeddingClient

async def main():
    vectors = await EmbeddingClient().embed_texts(["测试一下 PDF 文本是否能被 embedding"])
    print("vectors:", len(vectors))
    print("dim:", len(vectors[0]))
    print("first5:", vectors[0][:5])

asyncio.run(main())
'@ | python -
```

向量维度应与 `RAG_EMBEDDING_DIM` 相同。

### 17.3 查看 RabbitMQ

管理页：

```text
http://localhost:18213
```

重点检查：

```text
rag.ingest
rag.resolve
rag.cleanup
rag.dead
```

### 17.4 查看 Qdrant

```powershell
curl.exe http://localhost:18214/collections
curl.exe http://localhost:18214/collections/rag_chunks

curl.exe -X POST http://localhost:18214/collections/rag_chunks/points/scroll `
  -H "Content-Type: application/json" `
  -d "{\"limit\":5,\"with_payload\":true,\"with_vector\":false}"
```

Dashboard：

```text
http://localhost:18214/dashboard
```

### 17.5 查看 OpenSearch

```powershell
curl.exe http://localhost:18216/_cat/indices?v
curl.exe http://localhost:18216/rag_chunks/_count
curl.exe -X POST http://localhost:18216/rag_chunks/_search `
  -H "Content-Type: application/json" `
  -d "{\"size\":5,\"query\":{\"match_all\":{}}}"
```

## 18. 常见问题

### 18.1 API 返回 queued 后一直不处理

当前生产路径依赖独立 Worker。检查：

```powershell
docker compose ps rag-worker rabbitmq
docker compose logs rag-worker
```

再检查 `outbox_events` 是否停留在 `pending/publishing/dead`，以及 RabbitMQ 的 `rag.ingest`、retry queue 和 `rag.dead`。

### 18.2 Qdrant dashboard 沉默或无数据

检查 API 和 Worker 两侧配置均为：

```env
RAG_QDRANT_MOCK=false
```

Docker Compose 的 `environment` 会覆盖 `.env` 中同名配置。还要确认文档已经完成 OpenSearch 步骤并进入 `ready`；如果后续步骤失败，已写入 Qdrant 的 points 会被补偿删除。

### 18.3 OpenSearch 没有数据

检查：

```env
RAG_SEARCH_OPENSEARCH_MOCK=false
```

`true` 时使用进程内模拟器，不会出现真实 OpenSearch 索引。如果 OpenSearch 写入失败，任务应进入 `ingest_retrying`，Qdrant 和 MySQL chunks 也会被回滚。

### 18.4 Embedding API 返回权限错误

如果 OpenAI-compatible 服务返回 `Model.AccessDenied` 或 HTTP `401/403`：

1. 检查 API Key 是否属于当前 workspace；
2. 检查 workspace 是否已开通目标模型；
3. 检查 `RAG_EMBEDDING_BASE_URL` 中的 workspace 和区域；
4. 注意 `401/403` 默认是不可重试错误，会直接结束任务。

### 18.5 向量维度不匹配

错误可能来自 Embedding 客户端或 Qdrant 写入前校验：

```text
Embedding dimension mismatch
Vector dimension mismatch
```

确认 `RAG_EMBEDDING_DIM` 与模型真实输出一致。Qdrant collection 已按旧维度创建时，需要删除并重建 collection，或更换 `RAG_QDRANT_COLLECTION_PREFIX` 后重新入库。

### 18.6 PDF 有页面但没有 chunk

扫描件可能没有文本层。当前流程不做 OCR；所有页面都无法抽取文本时返回 `PDF_NO_TEXT_LAYER`。需要先对 PDF 做 OCR，或在抽取器中显式增加 OCR 分支。

### 18.7 任务处于 ingest_retrying

这是可恢复中间状态，不是成功状态。检查：

- `error_code` / `error_message`
- `attempt` / `max_attempts`
- `current_step` / `step_logs`
- RabbitMQ retry queue
- staging 原文件是否仍存在

下一次投递会先清理上次留下的 `failed` 文档，再从 staging 原文件重新执行完整入库流程。
