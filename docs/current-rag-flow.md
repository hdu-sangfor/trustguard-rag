# TrustGuard RAG 当前流程

> 文档快照：2026-07-25<br>
> 对应分支：`feature/rag-experience-knowledge`<br>
> 当前阶段：Phase 2.1 安全与业务边界加固已完成

本文总结当前系统从文档入库到检索、回答和引用校验的完整 RAG 流程。若本文与旧设计文档冲突，以当前源码为准。

## 1. 总体流程

```text
文档入库
  上传文件
    → 异步解析 / OCR
    → Token-aware 分块
    → 法规章节、条款元数据标注
    → Embedding
    → MySQL 保存权威数据
    → Qdrant 向量索引 + OpenSearch BM25 索引

在线问答 / 知识检索
  用户问题 + knowledge_base_id 或逻辑 scope
    → 查询规划
    → 多查询向量召回 + 多查询关键词召回
    → 融合
    → 知识库、文档状态及实体约束
    → 相邻 Chunk 扩展
    → Rerank
    → 文档级 Chunk 限制
    → Search 结果
    → 上下文构建
    → LLM 生成
    → JSON 与引用校验
    → 带来源回答或拒答
```

系统提供公开检索、内部检索和回答接口：

- `POST /v1/search`：只执行检索，返回知识片段、来源、得分和查询规划信息；生产环境要求 Agent Gateway 服务身份。
- `POST /v1/search/scope`：通过逻辑 Scope 执行联邦检索，供 Agent Gateway、普通 REST 和评测使用；复用共享 Scope 应用服务。
- `POST /v1/internal/knowledge/search`：要求内部 Bearer 服务身份，供 MCP Gateway 等受信服务调用；与公开 Search 复用相同应用服务。
- `POST /v1/internal/knowledge/search-scope`：供 MCP 以受信服务身份调用共享 Scope 应用服务。
- `GET /v1/internal/knowledge/resources/{resource_ref}`：校验 Scope 和来源版本后直接读取唯一来源 Chunk。
- `POST /v1/answer`：复用相同检索流程，再执行上下文构建、LLM 回答和引用校验。

## 2. 文档入库流程

### 2.1 文件解析

上传任务通过 Outbox 和 RabbitMQ 交给 Worker 异步处理。系统根据文件类型选择解析器；PDF、DOCX 和扫描件可经过 MinerU/OCR，最终统一形成带页码标记的纯文本。

### 2.2 分块和结构标注

解析文本按照 Token 数量分块，并保留重叠区域，避免语义在 Chunk 边界处完全断开。

对于新入库或重新入库的法规文本，系统会识别常见的：

- `第 X 章`
- `第 X 条`

并在 Chunk 元数据中写入：

- `chapter_no`
- `article_no`
- `content_type=legal_article`

这属于轻量结构标注，不是完整的法规语法解析。旧文档需要重新入库后才能获得这些新元数据。

### 2.3 数据和索引

每个 Chunk 会生成向量，并分别写入：

| 存储 | 作用 |
|---|---|
| MySQL | 文档、Chunk、任务状态和元数据的权威数据源 |
| Qdrant | 语义向量召回 |
| OpenSearch | BM25 关键词召回 |
| MinIO/BlobStore | 原文件、解析文本和相关产物 |

知识库绑定自己的 Embedding Profile。检索时必须使用相同 Profile，避免不同模型或向量维度混用。

## 3. 请求范围解析

单知识库检索必须携带 `knowledge_base_id`。后端首先：

1. 解析并校验知识库是否存在。
2. 取得该知识库的 Embedding Profile。
3. 把 `knowledge_base_id` 强制写入向量检索和关键词检索过滤条件。
4. 合并用户提供的其他文档、页码和元数据过滤条件。

因此知识库不是一个仅供前端展示的参数，而是后端检索隔离边界。所有召回、相邻 Chunk 扩展和文档状态检查都必须保持在这个范围内。

联邦检索不接受调用方提供物理知识库列表，而是接收逻辑 `scope`。`ScopeRegistry` 把 Scope
映射到允许的知识库集合，`KnowledgeApplicationService.search_scope` 统一执行逐库权限检查、
并发检索、RRF、配额、内容去重、Coverage 和 degraded 合并。MCP、公开 REST 和评测都调用
这一实现。

当前系统维持单租户 `workspace_id=default`，但应用层已经强制执行 Workspace、visibility 和
Workflow ABAC：workspace 内容必须匹配调用上下文，经验内容必须带 `workflow_type`，Scope
和已验证 Token 的 Workflow Allowlist 会共同收窄结果。非默认 Workspace 会 fail closed。

## 4. 查询规划

### 4.1 检索模式

查询规划器把问题分为三类：

| 模式 | 适用问题 | 目标 |
|---|---|---|
| `focused` | 单一事实、定义、时间、数量等 | 少量高相关证据 |
| `comprehensive` | 总结、比较、多实体、多阶段问题 | 多角度综合证据 |
| `enumeration` | “有哪些条款”“列出全部要求”等 | 尽可能提高覆盖率 |

请求中的 `retrieval_mode` 默认为 `auto`，也可以显式指定某种模式。

### 4.2 识别顺序

```text
显式指定模式
  → 直接使用

auto
  → 规则能够明确判断
      → 使用规则结果
  → 规则无法明确判断
      → 调用 LLM 判断意图并生成受控查询改写
  → LLM 未配置、超时、报错或置信度不足
      → 降级为 focused
```

LLM 只负责：

- 判断查询意图和范围；
- 生成最多 3 条语义查询改写；
- 生成最多 3 条关键词查询改写。

LLM 不负责决定 `top_k`、单文档 Chunk 上限等数值预算。其输出必须通过严格 JSON、枚举值、置信度和数量校验。有效规划会进入有界 TTL 内存缓存。

原始问题始终保留，查询改写只用于补充召回，不会替换原问题。

### 4.3 动态预算

未显式传参时，各模式采用以下预算：

| 参数 | `focused` | `comprehensive` | `enumeration` |
|---|---:|---:|---:|
| 最终 `top_k` | 系统默认值 | 15 | 20 |
| 向量召回上限 | 系统默认值 | 40 | 80 |
| 关键词召回上限 | 系统默认值 | 40 | 80 |
| 每篇文档最多 Chunk | 3 | 5 | 10 |
| Rerank 候选上限 | 系统默认值 | 25 | 50 |
| 回答上下文最多 Chunk | 系统默认值 | 12 | 20 |
| 相邻 Chunk 半径 | 0 | 1 | 2 |

如果请求显式提供 `top_k`、`vector_top_k`、`keyword_top_k` 或 `max_chunks_per_document`，显式值优先于自动规划值。

## 5. 多路召回

### 5.1 向量召回

系统对原问题及语义改写并发执行向量检索，用于召回表达不同但语义相关的内容。

### 5.2 关键词召回

系统对原问题及关键词改写并发执行 BM25 检索，用于召回法规名称、编号、专有名词和原文措辞。

### 5.3 查询变体合并

同一召回引擎内，不同查询变体命中的结果按 `chunk_id` 去重，并保留该 Chunk 的最高得分。向量和关键词组件可以配置有限重试。

如果两个启用的召回组件全部不可用，请求返回服务不可用；只有部分组件失败时，系统根据安全配置决定降级返回或拒答。

## 6. 融合、约束和重排

### 6.1 融合

向量和关键词结果支持两种融合方式：

- `rrf`：按两个结果列表中的排名进行 Reciprocal Rank Fusion，默认使用。
- `weighted_score`：归一化两类得分后按配置权重相加。

### 6.2 安全与有效性约束

融合后还会执行：

1. 只保留当前知识库中处于 `ready` 状态的文档。
2. 对 CVE、CWE、CAPEC 等安全实体执行识别和精确命中约束。
3. 在启用拒答策略时检查最低向量相似度。
4. 向量组件故障时，根据 `allow_keyword_fallback` 决定能否只使用关键词结果。

可能的拒答原因包括：

- `vector_unavailable`
- `no_exact_entity_match`
- `low_vector_score`

### 6.3 相邻 Chunk 扩展

`comprehensive` 和 `enumeration` 模式会把命中 Chunk 前后的相邻内容加入候选集：

- 扩展仍然限定在同一知识库、同一文档和有效 Chunk 内；
- 距离命中位置越远，初始分数越低；
- 扩展结果会继续参与 Rerank；
- 响应中的 `expanded=true` 表示该结果来自相邻扩展。

这个步骤用于补回被分块边界切开的上下文和相邻法规条款。

### 6.4 Rerank 和文档级限制

融合候选先进行文档级初步去重，再截取规划出的候选数量交给 Reranker。重排完成后，再应用最终的 `max_chunks_per_document` 和 `top_k`。

因此 `max_chunks_per_document` 是最终结果的文档多样性约束，而不是最初召回阶段的固定截断。枚举型问题会自动放宽该限制，避免一篇法规只能返回 3 条的情况。

## 7. 回答生成

`/v1/answer` 在 Search 结果之上继续执行以下步骤。

### 7.1 上下文构建

`ContextBuilder`：

1. 对检索结果去重；
2. 按查询规划确定最大上下文 Chunk 数；
3. 按 Token 预算顺序装入证据；
4. 给每条证据分配稳定的 `citation_id`；
5. 保留文档、页码、文件名、URI 和 Chunk ID。

如果检索结果为空，系统直接返回 `insufficient_evidence`，不会调用回答 LLM。

### 7.2 LLM 生成

LLM 接收：

- 系统回答规则；
- 用户原始问题；
- 结构化 `EVIDENCE_JSON`。

证据内容被视为不可信数据，模型不能执行证据中可能包含的指令。模型必须返回约定的 JSON：

```json
{
  "status": "answered",
  "answer": "回答正文。[1]",
  "citation_ids": [1]
}
```

模型也可以返回 `insufficient_evidence`，表示现有证据不足以支持回答。

### 7.3 引用校验和一次修复

服务端严格检查：

1. JSON 格式符合契约；
2. `answered` 回答至少包含一个引用；
3. 正文 `[n]` 与 `citation_ids` 一致；
4. 每个编号都来自本次实际提供的证据。

如果首次输出仅在 JSON 或引用契约上失败，系统允许一次受控修复。修复提示只允许调整 JSON、正文引用和 `citation_ids`，修复结果必须再次通过全部校验。两次调用的 Token 用量会合并统计。

这能验证引用编号和来源映射，但不能仅靠规则证明每句话都被引用内容充分支持，事实忠实度仍需评测和人工抽检。

## 8. 响应中的可观测信息

Search 和 Answer 响应会返回：

- `query_plan`：意图、规划来源、置信度、改写查询和实际预算；
- `search_status`：正常或降级；
- `effective_mode`：实际使用的向量、关键词或混合检索；
- `components`：各召回组件的结果数；
- `degraded_components`：发生故障的组件；
- `component_attempts`：组件实际尝试次数；
- `abstained` 和 `abstention_reason`：是否主动拒答及原因；
- `retrieval_time_ms`：检索阶段耗时；
- `coverage_status` 和 `coverage_warning`：枚举问题的覆盖声明。

查询规划来源可能是：

- `explicit`：调用方显式指定；
- `rule`：规则识别；
- `llm`：LLM 规划；
- `cache`：复用已缓存的 LLM 规划；
- `fallback`：LLM 不可用或结果不可信时降级；
- `disabled`：查询规划功能关闭。

### 8.1 Resource Ref 与精确回读

Scope Search 的新命中 URI 使用：

```text
trustguard-rag://{scope}/resources/{resource_ref}
```

`resource_ref` 是服务端使用 AES-GCM 签发的 `krf1.*` 不透明标识，绑定 Scope、物理知识库、
Chunk、来源版本和内容哈希。回读时应用服务直接定位唯一 `(knowledge_base_id, chunk_id)`，并
重新检查 Scope 映射、Workspace/Workflow 权限、文档 ready/active 状态、`source_revision`
和 `content_hash`。来源内容发生变化返回 `RESOURCE_STALE`；同一 Scope 内其他知识库更新不会
使该引用失效。

迁移期仍接受旧 URI：

```text
trustguard-rag://{scope}/chunks/{chunk_id}?revision={scope_revision}
```

新客户端应优先使用 Resource Ref。旧 URI 依赖 Scope 聚合 revision，仅用于 Phase 2 客户端
兼容，后续在迁移窗口结束后移除。

## 9. 枚举型问题的能力边界

对于“网络安全法包括哪些条款”这类问题，系统现在会提高召回预算、放宽单文档 Chunk 上限并扩展相邻内容，因此不会再被固定的 3 条文档配额过早截断。

但当前底层仍然是相关性检索，不是对法规结构执行确定性的全表扫描。因此：

- `enumeration` 响应固定返回 `coverage_status=partial`；
- 系统明确提示不能保证覆盖知识库中的全部条款或项目；
- 如果业务要求“完整且不漏项”，后续应基于 `article_no` 等结构化元数据增加专门的枚举执行器，而不能只继续增大 `top_k`。

## 10. 一次请求的简化时序

```text
浏览器/评测 ── Agent Gateway 服务 Token ── /v1/search 或 /v1/search/scope ─────┐
                                                                               │
Agent Runtime ── MCP OAuth ── rag-mcp ── MCP 内部服务 Token ── internal Scope ─┤
                                                                               ▼
                                              API Schema 校验
                                                     ▼
                              KnowledgeAccessContext + ApplicationService
                                                     ▼
知识库与 Embedding Profile 解析
  ▼
QueryPlanner
  ├─ 显式模式
  ├─ 规则
  ├─ LLM + 改写
  └─ focused 降级
  ▼
向量多查询 ─┐
            ├─ 去重 → 融合 → 状态/实体/分数约束
BM25 多查询 ─┘
  ▼
相邻 Chunk 扩展
  ▼
Rerank → 每文档 Chunk 限制 → top_k
  ├─ 单库 Search：公开和内部接口复用基础 Search 服务
  ├─ Scope Search：公开 REST、MCP 和评测复用 search_scope
  ├─ Resource Ref：直接定位来源并重新鉴权、校验版本
  └─ /v1/answer
       → Token 上下文预算
       → LLM JSON 回答
       → 引用校验 / 一次修复
       → 带来源回答或拒答
```

生产环境必须由反向代理或 API Gateway 屏蔽 `/v1/internal/*`，内部接口只允许服务网络中的
`rag-mcp` 等受信工作负载访问。`docker-compose.yml` 对 18200 的宿主机映射是本地开发拓扑，
不代表生产发布策略。
