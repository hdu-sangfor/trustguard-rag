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

## Embedding

默认轻量安装不包含本地推理依赖，`RAG_EMBEDDING_PROVIDER=pseudo` 可用于开发/测试。
生产建议接入 OpenAI-compatible embedding API；如需本地模型推理，再安装可选依赖：

```bash
uv sync --extra local-embedding
```

未设置 embedding 环境变量时，代码默认使用 `pseudo` provider，并按
`Qwen/Qwen3-Embedding-0.6B` 配置 `1024` 维向量；`.env.example` 则给出生产推荐的
OpenAI-compatible API 与 `text-embedding-v4` 示例。
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
frontend/       reserved for future UI
tests/
```

## 健康检查

- `GET /health/live` — 存活
- `GET /health` — 依赖详情
- `GET /health/ready` — ingest 模式下检查 mysql、qdrant、本地存储
