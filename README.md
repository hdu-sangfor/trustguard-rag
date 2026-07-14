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
docker compose up -d mysql qdrant opensearch redis rabbitmq minio
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
| opensearch | 18216 |
| minio | 18217 / 18218 |

本地 Compose 使用单节点 OpenSearch，并关闭安全插件，不能直接作为生产配置使用。
应用启动时会幂等地把 MySQL 中已有的 ready 文档分块回填到 OpenSearch，因而支持
在已有知识库之后再接入或重建 OpenSearch。可通过
`RAG_OPENSEARCH_BACKFILL_ON_STARTUP=false` 关闭启动回填。
若 Linux 上 OpenSearch 因 `vm.max_map_count` 过小而启动失败，可执行：

```bash
sudo sysctl -w vm.max_map_count=262144
```

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

处于 `staging`、`indexing` 或 `superseeding` 状态的文档仍由后台流程持有，删除请求会返回 `409`；
待文档进入终态后再执行删除，避免与向量和分块写入发生竞争。
删除成功时还会清理入库任务中的文档引用；清理失败会返回带 reference ID 的 `502`，
完整错误仅记录在服务端日志中。删除开始后文档先进入 `deleting`，Qdrant 和 OpenSearch
会被独立尝试删除；任一失败都会保留该状态，允许重复 DELETE 或应用启动时自动续跑。

文档只有在 Qdrant 与 OpenSearch 都写入成功后才会进入 `ready`。任一索引写入失败会将
任务标记为失败，并补偿删除另一侧索引、分块和 artifact，避免发布半成品文档。

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

## Rerank

默认关闭重排，避免轻量安装环境依赖未安装的本地 BGE 模型。使用百炼
`qwen3-rerank` 时配置：

```env
RAG_RERANK_PROVIDER=api
RAG_RERANK_MODEL=qwen3-rerank
RAG_RERANK_BASE_URL=https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-api/v1
RAG_RERANK_API_KEY=YOUR_BAILIAN_API_KEY
```

完整配置参见 [`docs/hybrid-search.md`](docs/hybrid-search.md)。

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
- `GET /health/ready` — ingest 模式下检查 mysql、真实启用的 qdrant/opensearch，以及对象或本地存储
