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
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
docker compose up -d mysql qdrant redis rabbitmq minio
uvicorn app.main:app --reload --port 18200
pytest
```

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
