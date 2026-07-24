# trustguard-rag-platform

TrustGuard 独立 RAG **知识库**服务。

## 快速开始（Docker）

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:18200/health
```

### 国内网络加速

`.env.example` 参考 `trustguard-agent` 统一配置了 Docker Hub、PyPI/uv 和
Hugging Face 国内镜像。执行 `docker compose build` 时，Compose 会把这些配置传给
Dockerfile；运行时下载 tokenizer 或本地嵌入模型则使用 `HF_ENDPOINT`。

```env
DOCKERHUB_REGISTRY=docker.m.daocloud.io
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1
```

如果镜像不可用，可将相应变量留空，恢复 Docker Hub、PyPI 和 Hugging Face 官方源。
需要走本机代理时可设置 `TRUSTGUARD_NETWORK_PROXY`；该代理只在镜像构建依赖安装阶段
使用，不会污染容器运行时访问 MySQL、Qdrant、OpenSearch 等内部服务的网络。

### 默认 MinerU 文档解析

PDF 和 DOCX 默认由 MinerU 解析。完整 Compose 会自动构建并启动 `mineru-api`：

```bash
docker compose up -d --build
```

首次构建 MinerU 需要安装解析依赖并下载模型，耗时及磁盘占用较大。默认 `pipeline`
后端使用 Python slim 基础镜像，不额外携带 vLLM 运行时；相关镜像源可用
`DOCKERHUB_REGISTRY`、`MINERU_BASE_IMAGE`、`UBUNTU_APT_MIRROR`、`PIP_INDEX_URL`
和 `MINERU_MODEL_SOURCE` 覆盖。默认只下载 `MINERU_MODEL_TYPE=pipeline` 对应模型，
避免把未使用的 VLM 模型打入镜像。
若只需本地 PDF 文本层 + 图片区域 OCR，可显式设置 `RAG_PDF_PARSER=local`；DOCX
仍需要 MinerU。

## 本地开发（Linux）

```bash
uv sync
docker compose up -d mysql qdrant opensearch redis rabbitmq minio
uv run uvicorn app.main:app --reload --port 18200
# 另开终端启动可靠任务 Worker
uv run python -m app.workers.main
uv run python -m pytest
```

依赖统一在 `pyproject.toml` 中声明，并由 `uv.lock` 锁定。修改依赖后运行
`uv lock` 更新锁文件；CI 或发布环境可用 `uv lock --check` 校验锁文件是否同步。

## 端口（182xx）

| 服务 | 端口 |
|------|------|
| rag-service | 18200 |
| mineru-api | 18220 |
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

## 入库（PDF / TXT / Markdown / DOCX）

先创建知识库并固定向量化模型，再向该知识库上传文档：

```bash
curl -X POST http://localhost:18200/v1/knowledge-bases \
  -H "Content-Type: application/json" \
  -d '{"name":"安全运营知识库","embedding_profile":"qwen3-embedding-0.6b"}'

curl -X POST http://localhost:18200/v1/ingest/jobs \
  -F "source_type=file" \
  -F "knowledge_base_id=<knowledge_base_id>" \
  -F "file=@report.pdf"

curl http://localhost:18200/v1/ingest/jobs/<job_id>
curl http://localhost:18200/v1/documents/<document_id>/chunks
```

PDF 和 DOCX 使用独立 MinerU API 解析为 Markdown；TXT 和 Markdown 由 RAG
按 UTF-8 直接读取（MinerU 本地 API 不接受这两种文本格式）。使用 Docker 全栈
启动时 MinerU 会自动启动，API 文档位于 `http://localhost:18220/docs`。

仅做本地 Python 开发、不运行完整 Compose 时，可单独启动 MinerU：

```bash
mineru-api --host 0.0.0.0 --port 8000
```

本机运行 RAG 时默认访问 `http://127.0.0.1:8000`；Docker 中通过服务名访问
`http://mineru-api:8000`。可通过 `RAG_MINERU_BASE_URL`、
`RAG_MINERU_DOCKER_BASE_URL`、
`RAG_MINERU_BACKEND` 和 `RAG_MINERU_TIMEOUT_SECONDS` 调整。当前 Word
支持范围为 `.docx`，旧式二进制 `.doc` 暂不支持。

## 知识库文档管理

模型在知识库创建时冻结，上传和检索只选择知识库，不能按请求临时更换模型。文档由入库任务创建；
入库后可通过文档 API 完成查询、更新和级联删除：

```bash
# 分页列表、关键词搜索与状态筛选
curl "http://localhost:18200/v1/documents?knowledge_base_id=<knowledge_base_id>&offset=0&limit=20&q=安全&status=ready"

# 查询详情
curl http://localhost:18200/v1/documents/<document_id>

# 更新标题、原始文件名或业务元数据
curl -X PATCH http://localhost:18200/v1/documents/<document_id> \
  -H "Content-Type: application/json" \
  -d '{"title":"企业安全指南","metadata":{"owner":"security"}}'

# 提交异步删除任务，并级联清理向量、分块和 artifact 文件
curl -X DELETE http://localhost:18200/v1/documents/<document_id>
```

处于 `staging`、`indexing` 或 `superseeding` 状态的文档仍由后台流程持有，删除请求会返回 `409`；
待文档进入终态后再执行删除，避免与向量和分块写入发生竞争。
删除接口返回 `202 Accepted`。文档会先进入 `deleting`，随后由 RabbitMQ Worker 独立
删除 Qdrant 和 OpenSearch 数据；任一失败都会保留该状态并进入延迟重试，超过上限后
进入 `rag.dead` 死信队列。双索引删除成功后才清理分块、artifact、任务引用和文档记录。

文档只有在 Qdrant 与 OpenSearch 都写入成功后才会进入 `ready`。任一索引写入失败会将
任务进入 `ingest_retrying` 并补偿删除另一侧索引、分块和 artifact，避免发布半成品文档；
Worker 会延迟重试，超过任务最大尝试次数后才标记为失败。
默认最多执行 3 次，并在第 3 次失败时立即终止，不会再等待额外队列投递。文件损坏、
无文本层、参数错误以及 Embedding API 的普通 4xx 属于不可重试错误；网络错误、429、
5xx 和索引后端临时故障才进入重试。

## 知识库检索

每次检索必须显式指定一个 `knowledge_base_id`。服务不会回退到默认知识库，也不再接受
用 `embedding_profile` 代替知识库范围；向量检索、关键词检索和最终文档状态校验都会使用
同一个知识库条件：

```bash
curl -X POST http://localhost:18200/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query":"如何防御 SQL 注入？",
    "knowledge_base_id":"<knowledge_base_id>",
    "enable_vector":true,
    "enable_keyword":true,
    "enable_rerank":false
  }'
```

查询中出现 CVE、CWE 或 CAPEC 编号时，服务会将大小写和常见分隔符规范化，例如
`cwe:89` 会转换为 `CWE-89`。OpenSearch 优先匹配结构化的 `entity_id`、
`entity_ids` 和 `aliases` 字段；融合与 rerank 完成后，主实体精确命中仍会稳定排在
关联实体和普通语义结果之前。检索结果会返回：

- `entity_id` / `entity_type`：文档主实体及类型；
- `entity_ids` / `entity_types`：当前分块包含的主实体和关联实体；
- `title` / `aliases`：用于检索展示和别名匹配；
- `exact_entity_match`：`primary`、`related` 或空值；
- `query_entities`：本次查询识别出的规范化安全编号。

启动时的 OpenSearch 与 Qdrant 回填会为已有文档补齐这些字段，新文档则在入库时直接写入。

融合和精确命中置顶完成后，服务会按 `document_id` 执行稳定去重，再把不同文档的代表
chunk 交给 rerank。请求参数 `max_chunks_per_document` 控制每篇文档最多保留多少个分块，
默认值为 `1`，允许范围为 `1` 到 `10`。响应中的 `deduplicated_chunks` 表示本次去除的
重复分块数量。若业务需要同一文档的多个上下文片段，可以显式调高该参数。

默认启用检索拒答：

- 查询含 CVE/CWE/CAPEC 时，如果当前知识库没有任何精确实体命中，返回空结果并设置
  `abstention_reason=no_exact_entity_match`；
- 普通语义查询按知识库所绑定 embedding profile 的校准阈值过滤，全部候选低于阈值时
  返回空结果并设置 `abstention_reason=low_vector_score`；
- `min_vector_score` 可覆盖模型默认阈值，`enable_abstention=false` 可用于诊断时关闭拒答。

向量和关键词组件临时失败时默认各额外重试 2 次，响应通过 `component_attempts` 返回实际
调用次数，通过 `recovered_components` 标识重试后恢复的组件。只有重试耗尽后才进入
`degraded`；可用 `component_max_retries` 按请求覆盖，或通过
`RAG_SEARCH_COMPONENT_MAX_RETRIES` 设置服务默认值。

## RabbitMQ Worker 与 Outbox

入库、删除和冲突解决均通过 Transactional Outbox 调度。API 在同一 MySQL 事务中保存
业务状态和 `outbox_events`，独立 Worker 再可靠发布到 RabbitMQ，因此 RabbitMQ 短暂不可用
不会丢任务。RabbitMQ 管理页为 <http://localhost:18213>。

```bash
# Docker 会同时启动 API 和 Worker
docker compose up -d --build

# 查看 Worker 与 RabbitMQ 状态
docker compose logs -f rag-worker
docker compose ps rabbitmq rag-worker
```

队列包括 `rag.ingest`、`rag.cleanup`、`rag.resolve` 和 `rag.dead`，失败命令按
10 秒、60 秒、300 秒退避。`RAG_WORKER_EAGER=true` 仅供自动化测试使用，生产环境禁止开启。
已有数据库无需清空 volume：API/Worker 启动时会幂等创建新增的 `outbox_events` 表。

## Embedding

代码在未设置 embedding 环境变量时默认使用 `pseudo` provider，确保执行 `uv sync`
的轻量本地开发环境可以直接运行。`.env.example` 为 Docker 部署选择本地模型，Docker
镜像会安装对应的可选依赖；在宿主机使用本地模型时需显式安装：

```bash
uv sync --extra local-embedding
```

`pseudo` provider 与本地 `Qwen/Qwen3-Embedding-0.6B` 均按 `1024` 维配置；
`.env.example` 默认选择该本地模型。生产环境也可配置远程 API。
用户侧 provider 只区分 `local` 与 `api`；API 协议由 `RAG_EMBEDDING_API_DRIVER`
区分 `openai_compatible` 与 `bailian`。Web 页面只在创建知识库时选择模型，随后上传和
检索都由知识库自动确定相同的向量空间。

百炼高精度配置为 `qwen3.7-text-embedding-2560` 和 `text-embedding-v4-2048`。
它们使用 `api + bailian` 原生驱动，分别生成 2560/2048 维稠密向量，并显式传递
`text_type=query|document` 和查询 `instruct`。不同 profile 使用独立 Qdrant collection；
同一 profile 下的不同知识库再通过 `knowledge_base_id` 强制隔离。OpenSearch 关键词检索
同样强制按知识库过滤，避免混合检索越界。
远程 Embedding 默认每批 10 条，并会根据兼容 API 返回的 batch-size 上限自动缩小批次。

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
RAG_EMBEDDING_API_DRIVER=openai_compatible
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
多格式入库与 OCR 参见 [`docs/ocr-and-multiformat-ingest.md`](docs/ocr-and-multiformat-ingest.md)。

## 目录结构（概要）

```
app/
  api/          health, knowledge_bases, ingest, documents, sources, ocr_review
  core/ingest/  extractors, pipeline, chunker, compensator
  core/ocr/     Paddle / API / custom OCR providers
  core/indexing/ qdrant_indexer
  core/embedding/ client
  stores/       db, blob, document, chunk, job, qdrant, ocr_region
  workers/      outbox publisher, RabbitMQ consumer, command handlers
docker/
  mysql-init.d/ 001_ingest.sql ... 004_knowledge_bases.sql
frontend/       知识库 Web 控制台
tests/
```

## 健康检查

- `GET /health/live` — 存活
- `GET /health` — 依赖详情
- `GET /health/ready` — 检查 MySQL、真实启用的 qdrant/opensearch，以及对象或本地存储；RabbitMQ 会报告但不阻止 API 接收 Outbox 任务
