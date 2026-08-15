# trustguard-rag-platform

TrustGuard 独立 RAG **知识库、混合检索与基于证据的回答**服务。

当前项目从启动、入库、MinerU/OCR 到混合检索和故障恢复的完整代码导览，参见
[`docs/project-code-logic.md`](docs/project-code-logic.md)。

## 快速开始（Docker）

```bash
cp .env.example .env
docker compose up -d --build
curl http://localhost:8200/health
```

开发环境默认允许直接调用 RAG 业务 REST。生产环境必须启用 Agent Gateway 服务身份，且
Gateway Token 与 MCP 内部 Token 必须使用不同的随机值：

```dotenv
RAG_GATEWAY_AUTH_ENABLED=true
RAG_GATEWAY_SERVICE_TOKEN=replace-with-a-different-long-random-service-token
RAG_INTERNAL_SERVICE_TOKEN=replace-with-a-long-random-service-token
RAG_RESOURCE_REF_SECRET=replace-with-another-random-secret-at-least-32-characters
```

启用后，`/v1/search`、`/v1/answer`、知识库、文档、入库和 OCR 接口都要求
`Authorization: Bearer <RAG_GATEWAY_SERVICE_TOKEN>`。健康检查和 `/v1/internal/*` 不使用
该身份；内部接口只接受独立的 `RAG_INTERNAL_SERVICE_TOKEN`。
生产环境还要求 `RAG_RESOURCE_REF_SECRET` 至少 32 字符，用于签发防篡改的不透明资源引用。
生产反向代理或 API Gateway 必须屏蔽 `/v1/internal/*`；Compose 暴露 8200 仅用于本地开发。

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
`DOCKERHUB_REGISTRY`、`MINERU_BASE_IMAGE`、`UBUNTU_APT_MIRROR`、
`MINERU_PIP_INDEX_URL` 和 `MINERU_MODEL_SOURCE` 覆盖。MinerU 默认单独使用官方
PyPI，避免第三方镜像中的 CUDA wheel 与索引哈希不一致；项目其他 Python 依赖仍可
使用全局 `PIP_INDEX_URL`。默认只下载 `MINERU_MODEL_TYPE=pipeline` 对应模型，
避免把未使用的 VLM 模型打入镜像。
若只需本地 PDF 文本层 + 图片区域 OCR，可显式设置 `RAG_PDF_PARSER=local`；DOCX
仍需要 MinerU。

## 本地开发（Linux）

```bash
uv sync
docker compose up -d mysql qdrant opensearch redis rabbitmq minio
uv run uvicorn app.main:app --reload --port 18200
# 通过 /v1/knowledge-scopes 配好 Scope 后，可独立启动只读 MCP Gateway
RAG_MCP_ENABLED=true uv run uvicorn app.mcp_server.main:app --port 18201
# 另开终端启动可靠任务 Worker
uv run python -m app.workers.main
uv run python -m pytest
```

依赖统一在 `pyproject.toml` 中声明，并由 `uv.lock` 锁定。修改依赖后运行
`uv lock` 更新锁文件；CI 或发布环境可用 `uv lock --check` 校验锁文件是否同步。

## 网络安全语料采集

Crawler 已整合到 RAG API、Outbox 和 RabbitMQ Worker。一次采集任务既可从直接 URL、
关键词搜索结果或站点入口发现网页，也可读取 NVD CVE、CISA KEV、MITRE CWE/CWE Views/
CAPEC 等官方结构化数据。原版 Crawler 的 OWASP Top 10:2021、ASVS 4.0.3 和 WSTG 4.2
产物，以及 NIST CSF 2.0、SP 800-53 Rev. 5 和国内法规/标准导航，作为明确标注版本的
内置基线提供。每条资料作为独立文档送入指定知识库；控制台的“数据采集”页面以分类
卡片为入口，并支持创建、暂停、继续、停止和查看任务。

控制台提供 9 个面向 TrustGuard Agent 工作流的知识分类预置：资产指纹与技术栈、漏洞与
弱点、漏洞检测与利用验证、修复处置与闭环、攻击技战术与攻击链、XDR 多源数据与检测、
工具与技能运行手册、威胁情报与实战案例、标准合规与报告依据。选择一个分类后，系统会
自动展开对应的结构化数据源、站点和关键词，并自动创建或复用同名 RAG 知识库。
例如选择“02 漏洞与弱点知识”会启用近 30 天 NVD、CISA KEV、CWE/CWE Views、CISA KEV
官方目录和 CVE 检索词。分类入口使用少量稳定的官方文档或列表页，不再自动混入全部新闻站点包。
选择“自定义采集”则不提交分类预置，也不绑定结构化来源或分类路由，仅使用用户填写的
直接 URL、关键词和站点入口写入所选知识库。

分类预置还会为每篇文档写入 `domain_category`、`kb_tier`、`agent_phases`、`topic_tags`、
`category_priority` 和 `crawler_preset_ids` 元数据，供 Agent 按阶段检索和后续过滤。旧版
11 个 `category_*` preset ID 会映射到语义最接近的新分类，但不再出现在控制台分类列表；
原 `knowledge_bases` 历史语料已迁移到相同的 9 类目录。混合任务会为未显式指定 `limit` 的
结构化来源设置公平上限，保留网页发现额度，避免前面的结构化来源耗尽
`max_total_pages`。单个采集任务只允许选择一个分类预置，但可以同时勾选额外的来源预置
或结构化数据源。

原版 Crawler 的 31 个互联网站点和 45 组检索词仍保留为 API 来源包，但不再默认并入分类卡片。
9 个分类分别使用经过检查的官方入口；控制台会自动填写该分类的站点和关键词。
`GET /v1/crawler/presets` 仍返回完整清单，便于 API 调用。

原仓库 `knowledge_bases` 目录中的历史 Markdown 语料可通过
`TrustGuard Legacy Markdown Corpus` 本地源分批导入。默认读取相邻仓库的
`../trustguard-crawler/knowledge_bases`，部署时应通过
`RAG_CRAWLER_LEGACY_CORPUS_ROOT` 指向只读挂载目录。当前目录包含 9 个分类、
2211 篇 Markdown；该兼容入口保留在 API 中，可按分类和起始偏移每批导入最多 200 篇。启用
`route_by_category` 后会按新分类名称自动创建或复用知识库，并将文档、去重记录和
入库任务路由到对应知识库。适配器拒绝根目录以外的分类和符号链接目标。

所有采集结果在进入 `rag.ingest` 前都会经过确定性清洗。清洗器会规范控制字符、空白、
Markdown 图片和安全情报中的 `hxxp`/`[.]` 写法；对 OWASP 页面额外清理 Front Matter、
HTML、链接目标和表格分隔符；对 CVE、CWE、CAPEC 记录过滤 `REJECTED`、`RESERVED`、
deprecated 和 revoked 条目。低于 `min_content_chars` 的内容不会入库，拒绝原因保存在
任务进度的 `rejections` 中。清洗版本和变更列表会写入文档元数据。
NVD、CWE、CAPEC 文档沿用原项目的 `cleaned_<ID>.md` 清洗产物命名；NVD 正文保留
漏洞描述、CVSS 评分/版本/向量、严重等级、CWE 和发布时间等标准字段。

```bash
curl -X POST http://localhost:18200/v1/crawler/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "knowledge_base_id": "<knowledge-base-id>",
    "preset_ids": [
      "government_security_agencies",
      "chinese_security_keywords"
    ],
    "urls": ["https://example.com/security-advisory"],
    "keywords": ["CVE supply chain attack"],
    "site_urls": [],
    "structured_sources": ["nvd", "cisa_kev", "cwe", "cwe_views", "owasp"],
    "source_options": {
      "nvd": {"days_back": 7, "limit": 20},
      "cwe": {"ids": ["CWE-79", "CWE-89"]},
      "cwe_views": {"ids": ["1000", "1003", "699"]}
    },
    "max_total_pages": 20,
    "min_content_chars": 80,
    "fetch_delay_seconds": 1,
    "max_retries": 2,
    "retry_base_seconds": 1,
    "route_by_category": false
  }'

curl http://localhost:18200/v1/crawler/jobs
curl http://localhost:18200/v1/crawler/sources
curl http://localhost:18200/v1/crawler/presets
curl http://localhost:18200/v1/crawler/legacy-corpus
```

### 数据源注册与周期增量采集

九类分类预置仍由采集页的小卡片选择。服务首次启动时会把九类预置幂等写入
`crawler_sources`，之后数据源地址、可信等级、内容类型、使用限制和调度周期均以 MySQL
配置为准；前端不需要增加结构化数据源表单。也可以通过管理 API 增加 URL、站点、RSS/Atom
或结构化来源：

```bash
curl -X POST http://localhost:8200/v1/crawler/registry \
  -H "Content-Type: application/json" \
  -d '{
    "id": "cisa-alert-feed",
    "knowledge_base_id": "<knowledge-base-id>",
    "name": "CISA Alerts",
    "source_kind": "rss",
    "endpoint": "https://www.cisa.gov/news.xml",
    "preset_ids": ["agent_08_threat_intelligence"],
    "trust_level": "official",
    "content_type": "threat_intelligence",
    "usage_restrictions": "保留来源与发布时间",
    "schedule_enabled": true,
    "schedule_interval_minutes": 360,
    "config": {"require_review": true, "review_mode": "human"}
  }'

# 立即触发一次增量采集；周期任务仍使用同一审核和入库链路
curl -X POST http://localhost:8200/v1/crawler/registry/cisa-alert-feed/runs \
  -H "Content-Type: application/json" -d '{}'

# 查看数据源级成功率、重复率、审核通过率和知识新鲜度
curl 'http://localhost:8200/v1/crawler/registry?include_stats=true'

# 查看内容版本、当前文档和被替代版本
curl http://localhost:8200/v1/crawler/registry/cisa-alert-feed/versions
```

增量 HTTP 采集会保存 `ETag` 和 `Last-Modified`，后续发送 `If-None-Match` 与
`If-Modified-Since`；`304 Not Modified` 不重复清洗或入库。正文哈希未变化时计为重复；哈希
变化时以 `conflict_policy=keep_new` 复用现有 Saga 发布新文档，并将旧文档和旧数据源版本标记
为已替代。每个数据源保留运行历史、资源状态和内容版本历史，周期调度按数据库中的
`schedule_interval_minutes` 执行。

采集页可直接选择“一次性采集”或开启“周期采集”，周期支持分钟、小时和天。开启后会先保存
数据源并立即执行首轮任务；分类卡片会展示已经启用的周期，关闭开关并再次提交可停用该分类的
后续调度。自定义周期采集会保存为 `custom` 数据源。

历史语料可按分类和偏移提交，例如：

```bash
curl -X POST http://localhost:18200/v1/crawler/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "knowledge_base_id": "<fallback-knowledge-base-id>",
    "structured_sources": ["legacy_corpus"],
    "source_options": {
      "legacy_corpus": {
        "category": "11_安全资讯与态势感知",
        "offset": 0
      }
    },
    "max_total_pages": 200,
    "route_by_category": true
  }'
```

采集命令使用独立的 `rag.crawl` 队列；采集成功后再扇出原有 `rag.ingest` 文档任务。
默认拒绝私网、环回、链路本地和云元数据地址，并在每次重定向后重新校验目标。生产环境
不要启用 `RAG_CRAWLER_ALLOW_PRIVATE_URLS`。`max_total_pages` 同时约束网页和结构化条目
总数；网页及官方 API 对网络异常和 429/502/503/504 响应执行指数退避重试。关键词、站点和
CWE 单条记录的失败会写入任务进度，但不会终止其他入口。暂停后继续时会重新扫描结构化来源
的稳定前缀，并跳过已经入库或已经被清洗器拒绝的 URL，避免漏掉断点之后的记录。可配置的上限、超时和重试参数
参见 `.env.example`。

Agent 自动驳回的清洗正文默认保留 30 天，期间可由人工查看并改判为通过；人工驳回仍会
立即删除暂存正文。Worker 按 `RAG_CRAWLER_REVIEW_CLEANUP_SCAN_SECONDS` 定期清理到期
内容，保留天数可通过 `RAG_CRAWLER_AGENT_REJECTION_RETENTION_DAYS` 调整。

## 端口

| 服务 | 端口 |
|------|------|
| rag-service | 8200 |
| rag-mcp（Streamable HTTP `/mcp`） | 8201 |
| mineru-api | 8220 |
| mysql | 8210 |
| redis | 8211 |
| rabbitmq | 8212 / 8213 |
| qdrant | 8214 / 8215 |
| opensearch | 8216 |
| minio | 8217 / 8218 |

宿主机端口均可通过 `.env` 中对应的 `*_HOST_PORT` 配置覆盖。默认使用 Windows
动态端口范围以下的 82xx，避免 Hyper-V、WSL 和 HNS 动态排除端口；容器之间仍使用
服务原生端口通信。

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
启动时 MinerU 会自动启动，API 文档位于 `http://localhost:8220/docs`。

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
  -H "Authorization: Bearer ${RAG_GATEWAY_SERVICE_TOKEN}" \
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

服务会先规划查询意图，再执行融合、rerank 和最终文档级去重。`retrieval_mode=auto`
时，高置信规则优先识别精准、综合和枚举问题，模糊问题可由配置的回答 LLM 生成受控的
语义/关键词改写；规划失败不会中断搜索。`max_chunks_per_document` 为空时按意图自动取
`3`、`5` 或 `10`，用户显式值始终优先。综合与枚举模式还会扩展命中分块的相邻内容。
响应中的 `query_plan` 会返回规划来源、改写和实际预算。

枚举模式会提高召回、rerank 和回答上下文预算，但仍属于相关性检索，因此响应会设置
`coverage_status=partial` 并明确提示不能保证覆盖全部条款。入库时会识别常见的“第 X
章/第 X 条”并保存结构元数据，后续可在此基础上增加严格的章节遍历。

默认启用检索拒答：

- 查询含 CVE/CWE/CAPEC 时，如果当前知识库没有任何精确实体命中，返回空结果并设置
  `abstention_reason=no_exact_entity_match`；
- 普通语义查询按知识库所绑定 embedding profile 的校准阈值过滤，全部候选低于阈值时
  返回空结果并设置 `abstention_reason=low_vector_score`；
- 启用了向量检索但向量组件故障时，默认返回空结果并设置
  `abstention_reason=vector_unavailable`；只有显式设置
  `allow_keyword_fallback=true` 才会返回纯关键词降级结果；
- `min_vector_score` 可覆盖模型默认阈值，`enable_abstention=false` 可用于诊断时关闭拒答。

向量和关键词组件临时失败时默认各额外重试 2 次，响应通过 `component_attempts` 返回实际
调用次数，通过 `recovered_components` 标识重试后恢复的组件。只有重试耗尽后才进入
`degraded`；可用 `component_max_retries` 按请求覆盖，或通过
`RAG_SEARCH_COMPONENT_MAX_RETRIES` 设置服务默认值。

## 只读 MCP Gateway

MCP Gateway 与 REST Core 使用同一镜像、独立进程和端口。它只提供
`knowledge_search` Tool 与一个不透明 Resource Ref Template，不开放上传、删除、回答生成或
经验写入。先通过 RAG Core 管理 API 将逻辑 Scope 映射到一个或多个知识库。映射和检索策略
保存在 MySQL，不再把可变业务策略编码进环境变量：

```bash
curl -X PUT http://localhost:18200/v1/knowledge-scopes/compliance \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <RAG_GATEWAY_SERVICE_TOKEN>' \
  -d '{"knowledge_base_ids":["<kb-id-1>","<kb-id-2>"],"default_mode":"comprehensive","per_knowledge_base_limit":20,"allowed_content_types":["legal_article","security_guide"],"allowed_workflow_types":["compliance"]}'
```

开发环境关闭 Gateway 鉴权时可省略 `Authorization`。`penetration-experience` 是系统绑定，
启用 Experience 后会自动加入 `penetration` Scope；管理 API 只能增删人工绑定，不能误删系统绑定。
Experience REST 与其他业务 REST 使用同一个 Agent Gateway 服务身份，不配置独立 Token 表。
浏览器用户的 `ADMIN/OPERATOR` 权限由 Agent Gateway 校验；RAG 服务必须只暴露在内部网络。

Compose 会在 `http://localhost:8201/mcp` 启动无状态 Streamable HTTP Server：

```bash
docker compose up -d --build rag-service rag-mcp
npx -y @modelcontextprotocol/inspector@latest --cli \
  http://localhost:8201/mcp --transport http --method tools/list
```

Agent 与 RAG 使用两套 Compose 时，Agent 默认通过
`http://host.docker.internal:8201/mcp` 访问；开发默认 Host 白名单已包含该地址，生产环境应
按实际服务域名覆盖 `RAG_MCP_ALLOWED_HOSTS`。

多知识库 Scope 只由 RAG Core 的 `KnowledgeApplicationService.search_scope` 解析和执行。
MCP 携带内部服务身份调用 `POST /v1/internal/knowledge/search-scope`；普通 REST 和评测调用
`POST /v1/search/scope`，三条路径复用同一套逐库授权、跨库 RRF、配额、去重、Coverage 和
降级语义。当前单租户固定为 `workspace_id=default`，Scope 与 JWT 中的 Workflow Allowlist
会继续收窄 workspace/经验内容。

新命中返回 `trustguard-rag://{scope}/resources/{resource_ref}`。`resource_ref` 不暴露物理
知识库或 Chunk ID，回读时直接定位唯一来源并校验来源 revision/content hash；无关知识库
更新不会使引用失效。服务尚未上线，因此不保留旧 Chunk URI、裸 Chunk 读取和内部单知识库
Search 兼容接口。`/health/live`、`/health/ready` 和 `/metrics` 用于独立探活和监控。

生产环境必须设置 `RAG_INTERNAL_SERVICE_TOKEN` 和 `RAG_MCP_AUTH_ENABLED=true`，并配置
issuer、audience 和 JWKS URL；缺少内部服务身份或启用 MCP 后未开启 OAuth 时启动失败。
Gateway 验证 Client Credentials 获取的短期 JWT，包括签名、`iss`、`aud`、`exp`、
OAuth scope 以及 `knowledge_scopes`；MCP 凭证不能用于管理接口和后续经验写入。
可选 `workspace_id` 和 `workflow_types` Claim 只在 JWT 验证后传入 RAG，非默认 Workspace
会被拒绝。

MCP Transport 只要求 JWT 通过认证，不在会话初始化时同时要求全部业务权限。
`knowledge_search` 单独要求 `rag.search`，Resource Read 单独要求
`rag.resource.read`；只执行搜索的客户端无需申请 Resource Read 权限。

## RabbitMQ Worker 与 Outbox

入库、删除和冲突解决均通过 Transactional Outbox 调度。API 在同一 MySQL 事务中保存
业务状态和 `outbox_events`，独立 Worker 再可靠发布到 RabbitMQ，因此 RabbitMQ 短暂不可用
不会丢任务。RabbitMQ 管理页为 <http://localhost:8213>。

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

## 基于证据的回答

`POST /v1/answer` 复用混合检索链路，将排序后的分块按 Token 预算组装为上下文，
再调用 OpenAI-compatible Chat Completions API。回答必须包含可映射到真实 Chunk 的
`[1]`、`[2]` 引用；请求必须显式提供 `knowledge_base_id`，并沿用与 `/v1/search`
相同的知识库隔离和拒答规则。空检索结果会直接返回 `insufficient_evidence`，不会调用
LLM。

```env
RAG_LLM_PROVIDER=openai_compatible
RAG_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RAG_LLM_API_KEY=YOUR_LLM_API_KEY
RAG_LLM_MODEL=qwen-plus
RAG_ANSWER_CONTEXT_MAX_TOKENS=6000
RAG_ANSWER_MAX_CONTEXT_CHUNKS=8
```

回答能力默认关闭，不影响只使用入库或 `/v1/search` 的部署。完整接口、错误语义和安全约束
参见 [`docs/answer-generation.md`](docs/answer-generation.md)。

## 目录结构（概要）

```
app/
  api/          health, knowledge_bases, ingest, documents, sources, search, answer, ocr_review
  core/ingest/  extractors, pipeline, chunker, compensator
  core/ocr/     Paddle / API / custom OCR providers
  core/indexing/ qdrant_indexer
  core/embedding/ client
  core/generation/ context builder, LLM client, citation validator, answer service
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
- `GET /health/ready` — 检查 MySQL、真实启用的 qdrant/opensearch、Qdrant
  `knowledge_base_id` 回填状态，以及对象或本地存储；RabbitMQ 会报告但不阻止 API
  接收 Outbox 任务
