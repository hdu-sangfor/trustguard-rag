# trustguard-rag-platform

TrustGuard 独立 RAG **知识库**服务。

## 快速开始（Docker）

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:18200/health
```

## 本地开发（Linux）

```bash
uv sync
docker compose up -d mysql qdrant redis rabbitmq minio
uv run uvicorn app.main:app --reload --port 18200
uv run python -m pytest
```

依赖统一在 `pyproject.toml` 中声明，并由 `uv.lock` 锁定。修改依赖后运行
`uv lock` 更新锁文件；CI 或发布环境可用 `uv lock --check` 校验锁文件是否同步。

## 端口（182xx）

| 服务 | 端口 |
|------|------|
| rag-service | 18200 |
| mysql | 18210 |
| redis | 18211 |
| rabbitmq | 18212 / 18213 |
| qdrant | 18214 / 18215 |

## 入库（PDF）

```bash
curl -X POST http://localhost:18200/v1/ingest/jobs \
  -F "source_type=file" \
  -F "file=@report.pdf"

curl http://localhost:18200/v1/ingest/jobs/<job_id>
curl http://localhost:18200/v1/documents/<document_id>/chunks
```

## 知识库文档管理

文档由入库任务创建；入库后可通过文档 API 完成查询、更新和级联删除：

```bash
# 分页列表、关键词搜索与状态筛选
curl "http://localhost:18200/v1/documents?offset=0&limit=20&q=安全&status=ready"

# 查询详情
curl http://localhost:18200/v1/documents/<document_id>

# 更新标题、原始文件名或业务元数据
curl -X PATCH http://localhost:18200/v1/documents/<document_id> \
  -H "Content-Type: application/json" \
  -d '{"title":"企业安全指南","metadata":{"owner":"security"}}'

# 删除文档，并级联清理向量、分块和 artifact 文件
curl -X DELETE http://localhost:18200/v1/documents/<document_id>
```

处于 `staging` 或 `indexing` 状态的文档仍由入库任务持有，删除请求会返回 `409`；
待文档进入终态后再执行删除，避免与向量和分块写入发生竞争。
删除成功时还会清理入库任务中的文档引用；清理失败会返回带 reference ID 的 `502`，
完整错误仅记录在服务端日志中。

## Embedding

代码在未设置 embedding 环境变量时默认使用 `pseudo` provider，确保执行 `uv sync`
的轻量本地开发环境可以直接运行。`.env.example` 为 Docker 部署选择本地模型，Docker
镜像会安装对应的可选依赖；在宿主机使用本地模型时需显式安装：

```bash
uv sync --extra local-embedding
```

`pseudo` provider 与本地 `Qwen/Qwen3-Embedding-0.6B` 均按 `1024` 维配置；
`.env.example` 默认选择该本地模型。生产环境也可按下文配置 OpenAI-compatible API。
Qdrant collection 会按该维度创建；更换模型或维度后需要重建 collection 并重新入库。

本地 Hugging Face 下载：

```env
RAG_EMBEDDING_PROVIDER=local
RAG_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RAG_EMBEDDING_DIM=1024
RAG_EMBEDDING_DOWNLOAD_SOURCE=huggingface
# 网络较慢时可开启镜像
# RAG_HUGGINGFACE_ENDPOINT=https://hf-mirror.com
# RAG_EMBEDDING_CACHE_DIR=./data/models/huggingface
```

本地 ModelScope 下载：

```env
RAG_EMBEDDING_PROVIDER=local
RAG_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RAG_EMBEDDING_DIM=1024
RAG_EMBEDDING_DOWNLOAD_SOURCE=modelscope
RAG_MODELSCOPE_ENDPOINT=https://www.modelscope.cn
RAG_MODELSCOPE_CACHE_DIR=./data/models/modelscope
```

OpenAI-compatible API：

```env
RAG_EMBEDDING_PROVIDER=api
RAG_EMBEDDING_BASE_URL=http://localhost:8080/v1
RAG_EMBEDDING_API_KEY=
RAG_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
RAG_EMBEDDING_DIM=1024
```

无模型的开发/测试环境可使用确定性伪向量：

```env
RAG_EMBEDDING_PROVIDER=pseudo
```

## 目录结构（概要）

```
app/
  api/          health, ingest, documents, sources
  core/ingest/  extractors, pipeline, chunker, compensator
  core/indexing/ qdrant_indexer
  core/embedding/ client
  stores/       db, blob, document, chunk, job, qdrant
  workers/      run_ingest_job
docker/
  mysql-init.d/ 001_init.sql, 001_ingest.sql
frontend/       知识库 Web 控制台
tests/
```

## 健康检查

- `GET /health/live` — 存活
- `GET /health` — 依赖详情
- `GET /health/ready` — ingest 模式下检查 mysql、qdrant、本地存储
