# TrustGuard RAG MCP 化与多 Workflow 接入计划

> 文档状态：Phase 0～2 已完成；Phase 2.1 安全与边界加固待实施；Phase 3 尚未实施<br>
> 文档日期：2026-07-24<br>
> RAG 基线分支：`origin/main`<br>
> RAG 基线提交：`93d08d0`<br>
> Agent 调研基线：`trustguard-agent/main@c8f3796`<br>
> 架构决策：[`adr/0001-rag-mcp-and-workflow-knowledge-boundary.md`](adr/0001-rag-mcp-and-workflow-knowledge-boundary.md)<br>
> v1 契约：[`../contracts/v1/README.md`](../contracts/v1/README.md)

## 1. 结论

随着 `trustguard-agent` 后续增加告警研判 Agent 等多个 Workflow，把知识能力抽象为 MCP 服务是合理的。但“存在多个 Workflow”本身并不是必须使用 MCP 的充分条件；MCP 的主要价值是跨 Agent Runtime、跨框架的工具发现、结构化契约、资源回读和后续受控的模型自主调用。

但不建议把 `trustguard-rag` 改成 **MCP-only**。推荐采用：

```text
RAG Core = Scope/ABAC、联邦检索、融合、版本和资源解析的权威业务内核
REST API = 管理面、前端、评测和普通系统接口
MCP      = 面向 Agent / Workflow 的只读北向协议适配层
```

目标架构：

```text
渗透测试 Workflow ─┐
                  ├─ 协议无关的 Agent KnowledgeGateway
告警研判 Workflow ─┤
未来其他 Workflow ─┘
                          │
                          ├─ 默认：TrustGuard MCP Client
                          └─ 可替换：受控内部 REST Client
                                  │
                                  ▼
                            rag-mcp 服务
                            ├─ MCP 协议协商
                            ├─ Transport 鉴权
                            ├─ MCP Tools / Resources
                            └─ 结构化协议适配
                                  │
                                  ▼
                    受服务身份保护的 Knowledge Application Service
                    ├─ Service Identity、Scope 和 Workspace ABAC
                    ├─ Scope → 知识库映射
                    ├─ 多知识库联邦检索、配额、RRF 和去重
                    ├─ Resource Ref 解析、版本和来源校验
                    └─ 调用 trustguard-rag 检索内核
                                  │
                                  ▼
                            trustguard-rag
                            ├─ 知识库管理
                            ├─ 文档入库 / OCR
                            ├─ 查询规划
                            ├─ 向量 + BM25
                            ├─ 融合 + Rerank
                            ├─ Workflow 独立经验知识库
                            ├─ 经验生命周期与效果反馈
                            └─ 来源与覆盖声明

各 Workflow 执行完成
        │
        ├─ 经验候选 / 使用反馈
        ▼
Agent Transactional Outbox
        │
        ▼
RabbitMQ → RAG Experience Consumer
        │
        ├─ 校验、脱敏、幂等
        ├─ 结构化经验存储
        └─ Qdrant + OpenSearch 索引
```

这样做可以让多个 Workflow 共用统一的工具发现、输入输出 Schema、鉴权、知识范围、引用和可观测性，同时保留现有前端、管理接口、评测脚本及普通 HTTP 调用方。

最终职责边界：

- MCP 只提供只读检索和来源读取，是北向协议适配层，不承担唯一业务权威；
- Scope、Workspace 隔离、联邦检索、资源解析和版本校验由 RAG Knowledge Application Service 统一实施，REST 和 MCP 不得各自形成不同语义；
- RAG REST 和可靠事件链路承担经验写入、反馈、审核及管理；
- Agent 保留任务运行态、原始证据、Checkpoint 和本地 `chk-*`；
- `trustguard-rag` 统一管理静态知识及各 Workflow 的长期经验知识；
- 每个 Workflow 拥有独立经验知识库，可按策略额外读取经过验证的共享经验库。

## 2. 决策依据

### 2.1 MCP 对多 Workflow 的价值

MCP 为 LLM 应用统一提供 Tools、Resources 和 Prompts。对于本项目：

- Tool 适合表达“根据问题检索知识”。
- Resource 适合表达“读取某个确定的知识 Chunk 或文档来源”。
- 不同 Workflow 可以发现并复用同一套能力。
- MCP Tool 可以被 LLM 自主调用，也可以由 Orchestrator 确定性调用。
- Tool 输入和结构化输出可以使用 JSON Schema 校验。
- 后续新增恶意样本分析、威胁狩猎、漏洞运营等 Agent Runtime 时，不需要重复开发 RAG 协议适配逻辑。

如果所有 Workflow 长期都运行在同一个 Orchestrator 中，并且始终由代码确定性调用，协议无关的共享 REST Client 也能满足复用要求。因此 Agent 必须依赖 `KnowledgeGateway` 抽象，而不能让业务代码直接依赖 MCP。这样既保留 MCP 的互操作价值，也允许在性能、兼容性或部署条件需要时切换到受控内部 REST。

MCP 官方把 Tools 定义为可执行或检索能力，把 Resources 定义为由应用管理的上下文数据；Tool 也可以返回结构化输出和 Resource Link：

- [MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP Resources Schema](https://modelcontextprotocol.io/specification/2025-06-18/schema#resources-read)
- [MCP Server 能力概览](https://modelcontextprotocol.io/specification/2025-06-18/server)

### 2.2 为什么不做 MCP-only

以下场景仍然更适合 REST：

- 知识库创建、删除和配置；
- 文档上传、任务状态和冲突处理；
- OCR 人工复核；
- Web 前端；
- OpenAPI 文档和 SDK；
- 现有检索、回答评测脚本；
- 经验候选写入、效果反馈、审核和状态管理；
- 运维、健康检查和批量管理；
- 不具备 MCP Client 的普通系统。

MCP 应当是 RAG 的北向能力适配层，而不是取代所有平台接口，也不能成为绕过 RAG Core 授权、联邦和版本规则的第二套业务内核。

### 2.3 为什么不让每个 Workflow 直接访问 Qdrant

直接共享 Qdrant 会把以下实现细节泄漏到所有 Workflow：

- Embedding Profile 和向量维度；
- Collection 和 Payload Schema；
- Qdrant 过滤语法；
- 文档 ready/active 状态；
- BM25、融合、Rerank 和拒答逻辑；
- 知识库隔离；
- 内容版本和迁移策略。

这会导致每个 Workflow 都重新实现一套不完整的 RAG。MCP Client 应依赖稳定的知识能力契约，而不是索引内部结构。

## 3. 当前系统约束

### 3.1 当前耦合与目标职责

Agent 当前的 `KBClient` 同时承担：

1. 静态知识检索；
2. 运行经验检索；
3. 运行经验写入。

当前实现可在迁移期保留，但长期职责应按数据生命周期拆分：

| 类型 | 示例 | 权威归属 |
|---|---|---|
| 任务运行态 | 当前 Todo、Plan、原始输出、临时证据、Checkpoint | `trustguard-agent` |
| 静态知识 | 法规、产品文档、安全手册、CVE、Playbook | `trustguard-rag` |
| Workflow 长期经验 | 已验证的适用条件、动作摘要、结果及有效性 | `trustguard-rag` 的 Workflow 独立经验库 |
| 跨 Workflow 通用经验 | 经过审核且可安全复用的通用方法 | `trustguard-rag` 的共享经验库 |

关键区别是：**任务记忆不等于经验知识**。原始执行记录继续留在 Agent；只有经过结构化、脱敏、可审计处理的经验候选才进入 RAG。Agent 当前的 Qdrant Experience Store 作为迁移期兼容读源，完成影子同步和效果验证后退出权威写入路径。

### 3.2 Workflow 经验知识库拓扑

建议按 Workflow 区分静态知识库和经验知识库：

```text
penetration-knowledge
penetration-experience

alert-triage-knowledge
alert-triage-experience

shared-experience       # 只保存审核通过的跨 Workflow 经验
```

逻辑 Scope 映射示例：

```text
penetration
  → penetration-knowledge
  → penetration-experience
  → shared-experience

alert-triage
  → alert-triage-knowledge
  → alert-triage-experience
  → shared-experience
```

检索时静态知识与经验可以统一召回和融合，但输出必须保留 `source_type`、`workflow_type` 和经验有效性，供 Agent 区分“文档事实”与“历史经验”。经验默认只在所属 Workflow 内可见，不能因为文本相似而跨 Workflow 泄漏。

### 3.3 Agent Chunk 引用约束

Agent 的 Plan 和 InstructionCompiler 只接受本地 Chunk Store 生成的：

```text
chk-<id>
```

RAG 当前返回 UUID Chunk ID。外部 UUID 不能直接写入 `context_chunk_refs`，否则会触发：

```text
CHUNK_INVALID_CHUNK_ID
```

因此 MCP 检索结果必须先在 Agent 内本地化：

```text
RAG external_chunk_id
       ↓
Agent task-local Chunk Store
       ↓
生成合法 chk-* ID
       ↓
写入 kb_hits / context_chunk_refs
```

### 3.4 MCP Server 无法直接读取 LangGraph State

MCP Server 是独立进程，不能直接访问 Agent 的 State、Store 或 Runtime Context。LangChain MCP 文档建议由客户端拦截器或调用层注入用户、Header 和运行时上下文：

- [LangChain MCP 文档](https://docs.langchain.com/oss/python/langchain/mcp)

因此：

- Workspace、Project、Workflow、Task 和 Trace 信息由 Agent MCP Client 注入；
- MCP Server 只信任经过验证的 Token Claim；
- 普通 Header 只用于路由提示和可观测性，不能单独作为授权依据。

## 4. MCP 服务部署形态

### 4.1 推荐独立 `rag-mcp` 服务

第一版不直接把 MCP ASGI App 挂进现有 `rag-service`，而是在同一仓库和镜像中增加独立入口：

```text
rag-service  18200  REST / 前端 / 管理 / 检索
rag-mcp      18201  Streamable HTTP /mcp
```

`rag-mcp` 调用受服务身份保护的内部知识接口：

```text
http://rag-service:18200/v1/internal/knowledge/search
```

现有 Phase 2 实现暂时调用普通 `POST /v1/search`，只适用于不包含 Workspace 私有数据的开发和验证环境。Phase 3 前必须迁移到内部受控接口；不能仅依靠网络位置或 MCP 外层 Token 保护 RAG 数据。

优点：

- MCP SDK 生命周期不会影响现有 FastAPI 生命周期；
- MCP 故障不会影响入库和前端；
- 可独立灰度、扩缩容和回滚；
- REST 与 MCP 契约边界清晰；
- 避免 MCP Session Manager 与现有迁移、回填任务共用生命周期；
- 后续如确认挂载方式稳定，可再合并进程。

额外 HTTP 跳转只发生在同一容器网络内，预计远小于 Embedding、检索和 Rerank 耗时，应通过性能测试确认。

部署约束：

- `rag-mcp` 只负责 MCP 协议转换、Transport 认证和协议级错误映射；
- Scope 映射、Workspace ABAC、联邦检索、配额、融合、去重和 Resource Ref 解析必须由共享 Knowledge Application Service 实施；
- MCP、普通 REST 和评测调用同一应用服务，避免结果语义漂移；
- 生产环境的 `rag-service` 内部检索端口不得直接暴露给非受信调用方；
- `APP_ENV=prod` 且 MCP 开启时，若 OAuth、内部服务身份或 Host/Origin 安全配置缺失，服务必须启动失败。

### 4.2 Transport

采用：

```text
MCP Streamable HTTP
stateless_http = true
json_response = true
endpoint = /mcp
```

不采用：

- 已被 Streamable HTTP 取代的旧 HTTP+SSE；
- 只适用于本地子进程的 stdio；
- 第一版不启用有状态 Session；
- 第一版不启用 Sampling、Elicitation 和 Roots。

当前 Tool 都是短时、请求—响应式只读操作，不需要 Server 主动推送和会话恢复。无状态模式也更容易水平扩展。

MCP 2025-06-18 规范定义 Streamable HTTP 使用单一 HTTP Endpoint，并要求客户端在初始化后发送协商出的协议版本：

- [MCP Streamable HTTP 规范](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

### 4.3 SDK

服务端和确定性客户端优先使用官方 Python SDK：

```text
modelcontextprotocol/python-sdk
```

要求：

- 锁定经过验证的 `1.x` 版本，不使用不受控浮动版本；
- 更新 `uv.lock`；
- 用 MCP Inspector 和自动化 Contract Test 验证；
- 不依赖非必要的第三方 MCP Server Runtime。

官方 SDK 支持 Streamable HTTP、Pydantic 结构化输出和 TokenVerifier：

- [MCP 官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)

Agent 后续若需要把 MCP Tool 直接绑定给 LangGraph LLM，可以增加 `langchain-mcp-adapters`。默认的决策前检索仍建议使用官方 MCP Client 封装，保证调用时机、参数、超时和结构化结果完全由 Orchestrator 控制。

## 5. MCP 能力设计

### 5.1 MVP Tool：`knowledge_search`

第一版只提供一个核心只读 Tool：

```text
knowledge_search
```

建议输入：

```json
{
  "query": "Apache Shiro RememberMe 识别后应验证哪些风险",
  "scope": "penetration",
  "mode": "comprehensive",
  "limit": 5,
  "rewrite": false,
  "filters": {
    "content_types": ["security_guide", "vulnerability"]
  }
}
```

字段规则：

| 字段 | 规则 |
|---|---|
| `query` | 必填，1～2000 字符，进入检索前再次脱敏 |
| `scope` | 使用逻辑别名，不暴露或信任原始知识库 ID |
| `mode` | `auto/focused/comprehensive/enumeration` |
| `limit` | 1～20；更大规模枚举需要专门能力 |
| `rewrite` | Agent 内部合成 Query 默认关闭 |
| `filters` | 只开放白名单字段，不允许任意底层元数据路径 |

Tool 标注：

```text
readOnlyHint    = true
destructiveHint = false
idempotentHint  = true
openWorldHint   = false
```

### 5.2 Tool 输出

使用 MCP Structured Content，并定义稳定 `outputSchema`：

```json
{
  "schema_version": "trustguard-knowledge-search-v1",
  "request_id": "req-...",
  "scope": "penetration",
  "status": "ok",
  "content_revision": "scope-revision",
  "hits": [
    {
      "external_chunk_id": "uuid",
      "resource_uri": "trustguard-rag://penetration/resources/krf-opaque",
      "resource_ref": "krf-opaque",
      "source_revision": 17,
      "content_hash": "sha256:...",
      "snippet": "Apache Shiro 的 RememberMe...",
      "score": 0.91,
      "title": "Shiro 安全检测指南",
      "document_id": "doc-uuid",
      "filename": "shiro-guide.pdf",
      "page_no": 12,
      "source_uri": "upload://shiro-guide.pdf",
      "source_type": "document",
      "workflow_type": null,
      "effectiveness": null,
      "visibility": "global",
      "expanded": false
    }
  ],
  "query_plan": {
    "intent": "comprehensive",
    "source": "explicit"
  },
  "coverage": {
    "status": "not_applicable",
    "warning": null
  },
  "degraded_components": [],
  "latency_ms": 631.2
}
```

注意：

- `status=degraded` 且仍有可信结果时，可以作为成功结果返回；
- 完全不可用时返回 MCP Tool Error；
- 缺失、无效、过期 Token 和 Transport 权限不足在 HTTP 边界返回 401/403；
- Tool/Resource 的知识 Scope 或 Workspace 业务授权失败返回稳定的 MCP 授权错误；
- 不向模型返回 API Key、内部地址、堆栈或数据库错误；
- 为兼容部分 Client，结构化结果同时提供简短 TextContent；
- TextContent 不能重复塞入所有完整正文，避免上下文翻倍。

### 5.3 MCP Resource

目标 Resource Template：

```text
trustguard-rag://{scope}/resources/{resource_ref}
```

其中 `resource_ref` 是服务端签发的不透明标识，内部至少绑定：

```text
knowledge_base_id
chunk_id
source_revision 或 content_hash
scope
```

调用方和模型不能解析、修改或自行构造其中的物理知识库标识。Phase 2 已实现的
`trustguard-rag://{scope}/chunks/{chunk_id}?revision={scope_revision}` 在迁移期保留兼容，
但不作为长期 Resource 身份模型。

用途：

- 精确读取某个搜索命中的完整 Chunk；
- Agent 将 Chunk 本地化为 `chk-*`；
- 最终报告回读引用；
- 恢复任务时校验来源是否仍存在；
- 避免 `knowledge_search` 把大量完整正文一次性塞入模型上下文。

Resource 返回：

```json
{
  "schema_version": "trustguard-knowledge-resource-v1",
  "scope": "penetration",
  "content_revision": "scope-revision-hash",
  "resource_ref": "krf-opaque",
  "source_revision": 17,
  "content_hash": "sha256:...",
  "chunk_id": "uuid",
  "document_id": "doc-uuid",
  "text": "完整 Chunk 文本",
  "title": "...",
  "filename": "...",
  "page_no": 12,
  "source_uri": "...",
  "metadata": {
    "article_no": "第十条",
    "content_type": "legal_article"
  }
}
```

规则：

- 只能读取调用方有权限的 Scope；
- Chunk 必须属于 Scope 当前映射的知识库；
- 文档必须处于 `ready`，Chunk 必须处于 `active`；
- `resource_ref` 必须同时校验 Scope、来源知识库、Chunk 和来源版本；
- 单个来源的 `source_revision/content_hash` 变化时返回明确的 stale 状态；
- Scope 中其他无关知识库更新不能让该 Resource 无条件失效；
- 不允许通过 URI 绕过知识库过滤；
- 联邦结果不能只按裸 `chunk_id` 去重，内部身份至少使用 `(knowledge_base_id, chunk_id)`，内容去重另用 `content_hash/document_id`；
- Resource 读取应直接定位单个来源，不得遍历 Scope 内所有知识库并返回第一个匹配项；
- `resources/list` 只列出调用方被授权的高层 Scope 或能力，不枚举全部 Chunk。

契约迁移规则：

- v1 保留现有必填字段；
- `resource_ref`、`source_revision` 和 `content_hash` 作为向后兼容的可选字段加入 v1；
- Agent 优先使用 `resource_ref`，缺失时才走旧 URI；
- 旧 URI 停止产生前必须经过 Agent 灰度和回滚验证；
- 如果需要删除或改变现有字段语义，则发布 v2，不修改已冻结的 v1 语义。

### 5.4 暂不提供的 MCP 能力

MVP 不提供：

- 文档上传；
- 文档删除；
- 知识库创建或删除；
- OCR 修改；
- 索引重建；
- 经验写入、效果反馈和状态变更；
- 任意 SQL/Qdrant 查询；
- Server Sampling；
- 自动执行文档中的命令；
- `knowledge_answer`。

这些能力要么属于管理面，要么会扩大攻击面。

经验写入不通过 MCP Tool 暴露给模型，而是通过受服务身份保护的内部 REST 或 Agent Outbox 事件完成，避免 LLM 直接写入、提升或发布经验。

`knowledge_answer` 可在后续作为独立只读 Tool 评估，但不能成为 Orchestrator 默认规划路径，否则会形成 RAG LLM 与 Workflow LLM 的嵌套调用。

## 6. Scope、知识库与联邦检索

### 6.1 使用逻辑 Scope

Workflow 不直接传递知识库 UUID，使用稳定别名：

```text
penetration
alert-triage
compliance
product-docs
threat-intelligence
```

逻辑名分为两类：

- Workflow View：如 `penetration`、`alert-triage`，表示由服务端组合出的知识视图；
- Knowledge Domain：如 `compliance`、`product-docs`、`threat-intelligence`，表示可独立授权和检索的知识领域。

v1 为了冻结契约和限制攻击面，继续使用显式 Scope 枚举。新增 Scope 时允许向 v1 增加枚举值，
但必须验证旧 Agent 的兼容行为；如果需要动态租户 Scope 或改变 Scope 语义，则发布 v2。
无论采用枚举还是后续注册表，调用方都只能使用服务端已配置且 Token 已授权的逻辑名。

服务端维护：

```text
Scope Alias
  → 一个或多个 knowledge_base_id
  → 静态库、Workflow 经验库和可选共享经验库
  → 允许的过滤字段
  → 默认检索模式和预算
  → 授权 Scope
```

LLM 不能自由构造或猜测知识库 ID。

租户型经验不要默认演变为“每个 Workspace × 每个 Workflow 一个知识库”。建议：

- 全局公共内容按 Workflow 建独立静态库和经验库；
- 少量必须物理隔离的租户使用独立 Workspace 知识库；
- 大量私有经验可共用 Workflow 经验库，但必须由服务端根据已验证 Token Claim 强制附加 `workspace_id` 和 `visibility` 过滤；
- 模型参数和普通 Header 不能关闭或扩大该过滤条件。

### 6.2 多知识库检索

告警研判可能同时需要：

- 威胁情报；
- SOC Playbook；
- 产品告警说明；
- 漏洞知识；
- 合规处置要求。

当前 RAG 单次请求只允许一个 `knowledge_base_id`。Phase 2 已在 MCP Gateway 中实现
Scope → 多 KB 并发调用和跨库 RRF，用于验证契约及效果；该实现属于过渡形态。

Phase 3 前应把联邦能力下沉到 RAG Knowledge Application Service，并提供受服务身份保护的受控接口。MCP、普通 REST 和评测脚本必须复用同一套 Scope 映射、过滤、配额、RRF、去重、Coverage 和 degraded 语义。MCP Gateway 不长期保留独立的联邦业务实现。

融合要求：

- 每个知识库独立检索；
- 不能直接比较不同检索空间的原始 Vector Score；
- 优先使用各库内部排名做跨库 RRF；
- 物理身份使用 `(knowledge_base_id, chunk_id)`，不能假定裸 `chunk_id` 跨库全局唯一；
- 内容重复使用 `content_hash/document_id` 去重，不与物理身份混为一谈；
- 每个 Scope 设置单库和总体配额；
- 输出保留每条命中的来源 Scope；
- 任一知识库故障时标记 degraded，而不是丢弃全部结果。

### 6.3 内容版本

RAG 需要新增单调递增：

```text
knowledge_bases.content_revision
```

以下操作真正完成后递增：

- 新文档完成双索引；
- 文档删除或替换；
- OCR 修订重新发布；
- 索引重建；
- Embedding Profile 迁移。
- 经验发布、更新、降级、归档或重新索引。

多 KB Scope 的搜索版本可表示为：

```text
sha256(sorted(kb_id + ":" + content_revision))
```

用途：

- Search Result 缓存失效；
- 联邦搜索结果回放和问题定位；
- 判断一次搜索所对应的 Scope 快照。

单个 Resource 另外携带来源级版本：

```text
source_revision 或 content_hash
```

用途：

- MCP Resource 精确过期检测；
- Agent 本地 Chunk 幂等键；
- Redis 检索缓存失效；
- 避免 Scope 内无关知识库更新导致所有 Resource 失效。

## 7. 鉴权与租户隔离

### 7.1 机器到机器认证

Agent 到 MCP 是无人值守的后台服务调用，适合 MCP 官方 OAuth Client Credentials 扩展：

```text
io.modelcontextprotocol/oauth-client-credentials
```

优先使用 JWT Bearer Assertion，生产环境不在仓库或普通 `.env` 中保存长期 Client Secret。

官方扩展明确适用于后台服务、Daemon 和 Server-to-Server：

- [MCP OAuth Client Credentials](https://modelcontextprotocol.io/extensions/auth/oauth-client-credentials)

### 7.2 Access Token Claim

建议 Token 包含：

```json
{
  "iss": "trustguard-auth",
  "sub": "trustguard-agent",
  "aud": "trustguard-rag-mcp",
  "scope": "rag.search rag.resource.read",
  "knowledge_scopes": [
    "penetration",
    "alert-triage"
  ],
  "exp": 0
}
```

规则：

- 每个 HTTP 请求都验证 Bearer Token；
- 验证签名、issuer、audience 和 expiry；
- Transport 层只要求访问 MCP 服务所需的基础权限；
- `knowledge_search` 单独要求 `rag.search`，Resource Read 单独要求 `rag.resource.read`，不能要求所有调用方同时拥有两者；
- Scope 参数必须同时存在于 Token 的 `knowledge_scopes`；
- 缺失、无效或过期 Token 在 HTTP 边界返回 401；
- Transport 级 OAuth 权限不足在 HTTP 边界返回 403；
- Tool/Resource 的 `knowledge_scopes` 或 Workspace 业务授权失败返回稳定的 MCP 授权错误，不伪装成检索错误；
- Access Token 使用短有效期并自动刷新；
- 使用 JWKS 验证，不在 MCP 服务分发私钥。

### 7.3 Workspace 和 Project

Workspace/Project 可能来自 Agent Runtime，但不能只相信自定义 Header。

原则：

- 知识库授权由 Service Identity + knowledge scope 决定；
- `X-Workspace-ID`、`X-Project-ID` 只能用于审计、Trace 和路由提示；
- 如果知识内容也按 Workspace 隔离，则 Token 必须包含可验证的 Workspace Claim；
- Knowledge Application Service 必须根据已验证 Claim 强制附加 `workspace_id`、`visibility` 和 Workflow 过滤；
- MCP Server、普通 REST、模型参数和普通 Header 都不能关闭或扩大这些过滤；
- 在内部检索接口和普通 REST 尚未实施同等 ABAC 前，不得接入 Workspace 私有知识或经验。

### 7.4 网络安全

Streamable HTTP 服务必须：

- 校验 `Origin`；
- 校验 Host Allowlist，防止 DNS Rebinding；
- 生产环境只暴露到内部网络或 API Gateway；
- 使用 TLS；内部环境可进一步启用 mTLS；
- 限制请求体大小；
- 设置连接、读取和总超时；
- 不把 `/mcp` 直接暴露到公网；
- 管理 REST 和 MCP 使用不同凭证与权限。
- `rag-service` 的内部 Search/Resource 接口只允许 `rag-mcp`、评测服务等受信服务身份访问；
- 对外 REST 若允许检索私有内容，必须执行与 MCP 相同的 Token Claim 和 ABAC，不能通过直接传递 `knowledge_base_id` 绕过 Scope；
- 开发环境允许关闭鉴权，生产环境必须 fail-fast，不能静默以无鉴权模式启动。

经验写入链路使用独立服务身份和最小权限：

```text
rag.experience.write
rag.experience.feedback
rag.experience.admin
```

MCP Access Token 按客户端实际用途最小化授予 `rag.search` 和/或 `rag.resource.read`，不能复用为经验写入凭证。`rag.experience.admin` 仅授予审核或策略服务，不授予 Workflow LLM。

MCP Transport 规范明确要求 Streamable HTTP 校验 Origin，并建议所有连接使用正确鉴权：

- [MCP Streamable HTTP 安全要求](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#security-warning)

## 8. Agent 侧共享知识接入层

### 8.1 不把 MCP 逻辑写进具体 Workflow

在 `trustguard-agent/orchestrator` 中新增共享层：

```text
app/knowledge/
  models.py
  policy.py
  query_builder.py
  mcp_client.py
  materializer.py
  gateway.py
  resilience.py
```

核心接口：

```python
class KnowledgeGateway:
    async def search(
        self,
        *,
        workflow: str,
        task_id: str,
        workspace_id: str | None,
        query: str,
        policy: KnowledgePolicy,
    ) -> KnowledgeSearchResult:
        ...
```

`KnowledgeGateway` 是 Agent 的稳定业务边界，MCP 只是其中一个 Transport Adapter。Workflow
不得直接创建 MCP Session、拼装 Tool 参数或解析 MCP 协议错误。这样可以在不改变 Workflow
的前提下进行 MCP 灰度、回滚，或在受控环境切换到内部 REST Adapter。

具体 Workflow 只负责：

- 什么时候检索；
- 如何构造脱敏 Query；
- 使用哪些 Scope；
- 默认 Mode 和预算；
- 知识依赖等级和失败时的决策；
- 如何把知识加入自己的 State。

`KnowledgePolicy` 至少定义：

```text
dependency = optional | required | safety-critical
```

- `optional`：知识是增强信息，超时或不可用时允许 fail-open；
- `required`：缺少知识时不得假装已完成研判，应拒答、延后或转人工；
- `safety-critical`：缺少或覆盖不足时禁止执行依赖该知识的高风险动作。

### 8.2 确定性调用与模型自主调用分离

提供两条使用路径：

```text
A. Orchestrator 确定性预检索
   → 关键决策前由代码调用 knowledge_search

B. LLM 自主深度检索
   → 将 knowledge_search 作为 Tool 暴露给模型
```

MVP 只实现 A。原因：

- 触发频率可控；
- 参数不由模型自由扩张；
- 更容易脱敏；
- 更容易做任务级缓存；
- 延迟和成本可预测；
- 不改变现有 PlanList 工具执行体系。

路径 B 在基础链路稳定后再开启，并通过 MCP Tool Filter 只暴露允许的只读 Tool。

### 8.3 MCP Client

确定性路径优先直接使用官方 MCP Python Client，不依赖 LLM Tool Binding。

职责：

- 初始化并协商协议能力；
- `tools/list` 校验 Tool 和 Schema 版本；
- 调用 `knowledge_search`；
- 读取 Resource；
- 传递 Authorization 和 Trace Header；
- 严格校验 Structured Content；
- 超时、取消、有限重试和熔断；
- 根据 `KnowledgePolicy.dependency` 执行 fail-open、拒答、延后或转人工，不能全局固定 fail-open。

如后续使用 `langchain-mcp-adapters`：

- 仅在需要模型自主调用时使用；
- 使用 Interceptor 注入身份和 Trace；
- 配置 Tool Allowlist；
- 验证 Resource Link 和 Structured Content 是否被完整保留；
- 不允许 MCP Server直接写入 LangGraph State。

## 9. 外部 Chunk 本地化

### 9.1 本地化流程

```text
knowledge_search 返回 Hit + resource_uri
       ↓
Agent 按选择策略读取 Resource
       ↓
写入 task-local Chunk Store
       ↓
生成 chk-* ID
       ↓
kb_hits 暴露本地 ID 和短摘要
       ↓
PlanList 引用 chk-*
       ↓
InstructionCompiler 读取和校验
```

### 9.2 Agent Chunk 元数据

```json
{
  "chunk_type": "rag_knowledge",
  "retention": "ephemeral",
  "tenant_id": "workspace-id",
  "provider": "trustguard-rag-mcp",
  "scope": "penetration",
  "external_chunk_id": "uuid",
  "resource_uri": "trustguard-rag://...",
  "resource_ref": "krf-opaque",
  "source_revision": 17,
  "content_hash": "sha256:...",
  "document_id": "doc-uuid",
  "filename": "guide.pdf",
  "page_no": 12,
  "source_uri": "upload://guide.pdf",
  "content_revision": "scope-revision-hash",
  "retrieved_at": "..."
}
```

### 9.3 幂等与生命周期

任务内幂等键：

```text
优先：sha256(scope + resource_ref + source_revision/content_hash)
兼容：sha256(scope + external_chunk_id + content_revision)
```

规则：

- 同一任务重复命中同一个外部 Chunk 时复用本地 `chk-*`；
- 默认使用 `ephemeral`；
- 被 Plan 引用后依赖现有 ref_count 防止提前 GC；
- 最终报告引用的重要 Chunk 可提升为 `pinned`；
- 原始来源和 revision 随 Chunk 保存；
- 不把 MCP Session ID 当业务持久化标识。

### 9.4 来源引用模型

区分：

```text
ContextChunkRef    = Agent 执行期本地上下文引用
KnowledgeSourceRef = 最终报告中的外部知识来源
```

`KnowledgeSourceRef`：

```json
{
  "provider": "trustguard-rag",
  "scope": "penetration",
  "local_chunk_id": "chk-...",
  "external_chunk_id": "uuid",
  "resource_ref": "krf-opaque",
  "source_revision": 17,
  "content_hash": "sha256:...",
  "document_id": "doc-uuid",
  "filename": "guide.pdf",
  "page_no": 12,
  "source_uri": "upload://guide.pdf",
  "content_revision": "scope-revision-hash"
}
```

## 10. 渗透测试 Workflow 策略

### 10.1 触发条件

渗透 Workflow 在以下事件调用 MCP：

- 任务首次进入需要规划的阶段；
- Phase 变化；
- Todo 变化；
- 发现新的技术栈；
- 识别新的 CVE/CWE/CAPEC；
- 当前计划受阻并允许一次知识补充；
- 进入报告和修复建议阶段；
- 用户直接提出知识问题。

不在以下情况调用：

- Executor 正在执行既定工具；
- 只保存 Checkpoint 或 Trace；
- 同一知识指纹已经检索；
- 当前步骤由确定性规则完全决定；
- MCP 熔断期间。

### 10.2 默认策略

```json
{
  "scope": "penetration",
  "mode": "comprehensive",
  "limit": 5,
  "rewrite": false,
  "fail_open": true
}
```

Agent 已经根据 Phase、Todo、技术栈和历史构造 Query，因此内部规划 Query 默认关闭 RAG LLM 改写。

普通用户问答可使用：

```json
{
  "mode": "auto",
  "rewrite": true
}
```

### 10.3 查询指纹

```text
workflow
+ task_id
+ phase
+ todo_id
+ detected_technologies
+ detected_security_entities
+ scope_revision
```

指纹不变时复用已有结果，避免每个 Decision Tick 都调用 MCP。

## 11. 告警研判 Workflow 策略

### 11.1 告警研判需要的知识

建议 Scope：

```text
alert-triage
threat-intelligence
product-docs
response-playbooks
compliance
```

知识来源：

- 告警规则和字段说明；
- 产品处置手册；
- IOC 和威胁情报；
- ATT&CK 技术说明；
- CVE/CWE 知识；
- 历史误报模式；
- SOC Playbook；
- 合规报告和通知要求。

历史误报和组织内部处置经验进入 `alert-triage-experience`，但必须携带 `workspace_id` 和 `visibility`，由 RAG 服务端强制隔离。Agent 只保留本次研判的原始日志、证据和任务状态；未经脱敏、结构化或验证的内容不能直接成为可检索经验。

### 11.2 触发条件

- 新告警进入；
- 告警实体抽取完成；
- 发现新的 Hash、Domain、IP、进程、命令行、CVE 或规则 ID；
- 需要判断攻击阶段和影响；
- 需要查询产品字段或规则含义；
- 初始证据不足，需要一次补充检索；
- 生成处置建议；
- 生成研判报告和引用。

### 11.3 Query 构造

禁止直接把完整原始日志、Token、Cookie、认证头或大段命令输出发送到 RAG。

先结构化抽取：

```json
{
  "alert_type": "webshell",
  "rule_name": "suspicious_php_process",
  "product": "xdr",
  "entities": {
    "process": "php-fpm",
    "child_process": "sh",
    "file_hash": "...",
    "cve": []
  },
  "question": "该行为常见攻击链、误报条件和处置步骤是什么"
}
```

再生成短 Query：

```text
XDR suspicious_php_process 告警中 php-fpm 拉起 sh，
常见攻击链、误报条件、验证证据和处置步骤是什么？
```

### 11.4 检索模式

| 情境 | Mode |
|---|---|
| 查询规则字段、CVE 定义、单个 IOC | `focused` |
| 综合研判攻击链、误报条件和处置步骤 | `comprehensive` |
| 列举某类告警全部处置检查项 | `enumeration` |

告警研判默认：

```json
{
  "scope": "alert-triage",
  "mode": "comprehensive",
  "limit": 8,
  "rewrite": false,
  "fail_open": true
}
```

## 12. Workflow 经验知识管理

### 12.1 经验不是普通文档

经验不能只被拼成 Markdown 或 PDF 再走文档上传。RAG 应提供原生结构化经验模型，同时生成一份适合检索的文本投影。

建议核心表：

```text
experience_items
experience_feedback_events
experience_status_history
```

`experience_items` 至少包含：

```text
id
external_id
source_system
source_revision
knowledge_base_id
workflow_type
experience_type
workspace_id
visibility
status
effectiveness
conditions
action_summary
outcome_summary
skill_id
phase
source_task_id
usage_count
success_count
failure_count
quality_score
created_at
updated_at
expires_at
```

其中：

- `conditions` 表达经验的适用前提，不满足条件时不能直接推荐；
- `action_summary` 只保存可复用的方法摘要，不保存未经处理的完整命令输出；
- `outcome_summary` 和 `effectiveness` 表达结果与可信度；
- `source_task_id` 用于审计，不应作为检索正文；
- 敏感原始证据留在 Agent 或证据系统，RAG 只保存受控引用。

### 12.2 生命周期

```text
candidate → pending → proven → deprecated → archived
```

规则：

- `candidate`：Workflow 刚产生、仅完成基本 Schema 校验；
- `pending`：已脱敏和去重，等待更多运行反馈或人工审核；
- `proven`：满足证据、成功率、样本量和审核策略，可正常参与检索；
- `deprecated`：已被证明过时、风险升高或被新经验替代；
- `archived`：只保留审计，不参与检索。

默认只召回 `proven`。`pending` 可在 Shadow 模式或降低权重后召回，`candidate/deprecated/archived` 不进入正常检索。状态提升由确定性策略或人工审核完成，LLM 不能直接把经验标记为 `proven`。

### 12.3 写入与反馈接口

建议内部 REST：

```text
PUT   /v1/experiences/{external_id}
POST  /v1/experiences/{experience_id}/feedback
PATCH /v1/experiences/{experience_id}/status
GET   /v1/experiences/{experience_id}
GET   /v1/experiences
```

经验候选写入示例：

```json
{
  "source_system": "trustguard-agent",
  "source_revision": 3,
  "workflow_type": "penetration",
  "experience_type": "skill_outcome",
  "workspace_id": "ws-...",
  "visibility": "workspace",
  "conditions": {
    "skill_id": "http-fingerprint",
    "technology": "Apache Shiro"
  },
  "action_summary": "先通过响应特征确认 RememberMe，再执行无害验证。",
  "outcome_summary": "在目标环境中减少了误报。",
  "effectiveness": "candidate",
  "source_task_id": "task-..."
}
```

要求：

- 写入使用 Service Token 和 `Idempotency-Key`；
- 数据库唯一约束为 `(source_system, external_id)`；
- `source_revision` 只允许新版本覆盖旧版本，迟到事件不能回滚内容；
- Feedback 事件必须有唯一 `event_id`，重复投递不重复计数；
- 状态修改使用 `rag.experience.admin`，普通 Workflow 只能提交候选和反馈；
- 写入响应表示“权威数据已持久化”，索引状态单独返回，不伪装为同步完成。

### 12.4 生产写入链路

```text
Workflow 生成结构化经验候选
        ↓
Agent 本地 Schema 校验与脱敏
        ↓
与任务结果同事务写入 MySQL Outbox
        ↓
Outbox Publisher → RabbitMQ
        ↓
RAG Experience Consumer
        ↓
权限校验 / 幂等 / 去重 / 敏感信息检查
        ↓
经验数据库
        ↓
生成检索文本投影
        ↓
Qdrant + OpenSearch 双索引
        ↓
递增 knowledge_base.content_revision
```

REST `PUT` 用于管理、补偿、回放和低流量调用；生产 Workflow 优先使用 Outbox + RabbitMQ，避免 Agent 任务成功但经验写入丢失。Consumer 必须支持至少一次投递、幂等消费和死信队列。数据库成功而索引失败时进入可重试的 `index_pending` 状态，由补偿任务完成双索引后才对正常检索可见。

### 12.5 效果反馈与晋级

Agent 在经验被实际使用后发送反馈：

```json
{
  "event_id": "evt-...",
  "task_id": "task-...",
  "workflow_type": "penetration",
  "outcome": "success",
  "evidence_level": "verified",
  "notes": "适用条件匹配，验证成功"
}
```

RAG 更新 `usage_count/success_count/failure_count/quality_score/effectiveness`。晋级策略至少考虑：

- 最小使用次数；
- 成功率与失败率；
- 反馈是否有可验证证据；
- 是否来自多个独立任务；
- 是否发生条件漂移或过期；
- 是否需要人工审核。

单次成功不能自动晋级为 `proven`，单次失败也不应直接删除；持续失败、版本变化或安全策略变化应触发降级或重新审核。

### 12.6 迁移策略

Agent 当前 Qdrant Experience Store 不立即删除：

1. RAG 经验模型和写入链路上线；
2. 新经验双写或通过 Outbox 同步到 RAG；
3. RAG 经验检索保持 Shadow，对比召回和决策效果；
4. 将历史经验回填为结构化记录并标记来源；
5. 逐 Workflow 切换经验读取到 RAG；
6. 保留可回滚窗口；
7. 停止 Agent 旧 Experience Store 写入，最终下线旧读路径。

迁移期间必须指定唯一权威状态，避免 Agent 与 RAG 双向写入形成冲突。推荐从第 2 步开始以 RAG 数据库为长期经验权威，Agent 旧库仅作为兼容读副本。

## 13. RAG 仓库改造

### 13.1 契约与数据模型

- [ ] 新增 `content_revision`；
- [ ] 为 Search Response 增加稳定 `schema_version`；
- [ ] 将 `query_plan` 从自由 `dict` 改为 Pydantic Schema；
- [ ] 将 coverage 状态改为枚举；
- [ ] 增加稳定错误 Envelope；
- [ ] 增加精确按 Chunk ID 读取接口，强制知识库隔离；
- [ ] 增加 Scope 配置模型；
- [ ] 为多知识库 Scope 准备 revision 聚合；
- [ ] 为 Hit 和 Resource 增加可选 `resource_ref/source_revision/content_hash`；
- [ ] 输出稳定的来源字段；
- [ ] 明确文本长度和元数据上限。
- [ ] 增加原生 `experience_items`、反馈事件和状态历史模型；
- [ ] 增加经验状态、可见性、Workflow、Workspace 和有效性字段；
- [ ] 为经验生成可版本化的检索文本投影；
- [ ] Search Hit 输出 `source_type/workflow_type/effectiveness/visibility`；

### 13.2 鉴权

- [ ] 增加内部 REST Service Token；
- [ ] 新增受服务身份保护的内部 Knowledge Search 接口；
- [ ] 新增 OAuth/JWT TokenVerifier；
- [ ] 实现 MCP Client Credentials 扩展；
- [ ] 校验 issuer/audience/expiry/scope；
- [ ] Search 与 Resource Read 独立授权，遵循最小权限；
- [ ] 实现 knowledge scope 授权；
- [ ] 在 Knowledge Application Service 强制实施 Workspace、visibility 和 Workflow ABAC；
- [ ] 普通 REST 不得通过任意 `knowledge_base_id` 绕过 Scope 和 Workspace 授权；
- [ ] 增加 Origin 和 Host Allowlist；
- [ ] 管理接口与检索接口分权；
- [ ] 增加 `rag.experience.write/feedback/admin` 并使用独立服务身份；
- [ ] 增加审计日志。

### 13.3 MCP Server

- [ ] 新增 `app/mcp_server/`；
- [ ] 定义 Pydantic Input/Output；
- [ ] 实现 `knowledge_search`；
- [ ] 实现不透明 Resource Ref Template，并兼容旧 Chunk Resource URI；
- [ ] 只启用 Tools、Resources 和必要 Logging；
- [ ] 禁用 Sampling、Roots 和 Elicitation；
- [ ] 实现受服务身份保护的 Knowledge Application Service Client；
- [ ] MCP 层不长期保留独立的跨知识库 RRF 业务逻辑；
- [ ] 实现结构化错误映射；
- [ ] 增加 `/health/live` 和 `/health/ready`；
- [ ] 增加独立启动入口；
- [ ] 更新 Docker Compose。

### 13.4 Knowledge Application Service

- [ ] 统一 Scope → KB 映射；
- [ ] 统一 Service Identity、knowledge scope 和 Workspace ABAC；
- [ ] 统一联邦检索、单库/总配额、RRF、去重、Coverage 和 degraded 合并；
- [ ] 统一 Resource Ref 签发、解析、来源版本和 active/ready 校验；
- [ ] 为 MCP、普通 REST 和评测提供相同业务语义；
- [ ] 生产内部接口只允许受信服务身份访问。

### 13.5 经验写入与索引

- [ ] 实现经验 Upsert、Feedback、Status 和审计查询接口；
- [ ] 实现 `Idempotency-Key`、唯一事件和 `source_revision` 防旧写覆盖；
- [ ] 实现 RabbitMQ Experience Consumer 和死信队列；
- [ ] 实现数据库 Outbox/索引补偿任务；
- [ ] 双索引完成后再切换为可检索状态；
- [ ] 经验状态变化时递增 `content_revision`；
- [ ] 实现候选去重、过期、降级和归档；
- [ ] 实现 Workspace 强制过滤和跨 Workflow 隔离。

### 13.6 安全

- [ ] 对 Query 二次脱敏；
- [ ] 限制 Tool 参数长度；
- [ ] 限制 Resource 文本大小；
- [ ] 把知识内容标记为不可信数据；
- [ ] 禁止把知识内容作为 MCP 指令；
- [ ] 不记录凭证；
- [ ] 对外部错误脱敏；
- [ ] 增加请求频率和并发限制。

## 14. Agent 仓库改造

### 14.1 KB 职责拆分

- [ ] 协议无关的 `KnowledgeGateway`；
- [ ] 新增 `McpKnowledgeTransport`；
- [ ] 可选的 `InternalRestKnowledgeTransport`，只用于受控部署和回滚；
- [ ] 联邦检索不在 Agent 重复实现，由 RAG Knowledge Application Service 负责；
- [ ] 保留任务态 `ChunkStore`、Checkpoint 和原始证据存储；
- [ ] 新增 `ExperienceCandidatePublisher`；
- [ ] 新增 `ExperienceFeedbackPublisher`；
- [ ] 使用 Transactional Outbox 可靠发布经验事件；
- [ ] `QdrantExperienceStore` 改为迁移期 `LegacyQdrantExperienceRetriever`；
- [ ] 原有 Agent 静态 KB 作为灰度兼容实现。

### 14.2 共享知识接入层

- [ ] Transport Adapter 生命周期；
- [ ] MCP Client 生命周期；
- [ ] Token 获取和刷新；
- [ ] Structured Content 校验；
- [ ] Tool/Schema 版本校验；
- [ ] Header 和 Trace 注入；
- [ ] Retry、Timeout 和 Circuit Breaker；
- [ ] optional/required/safety-critical 失败策略；
- [ ] Resource 读取；
- [ ] Chunk 本地化；
- [ ] 任务级幂等；
- [ ] `KnowledgeSourceRef`。

### 14.3 Workflow 抽象

- [ ] `KnowledgePolicy`；
- [ ] `KnowledgeTrigger`；
- [ ] `KnowledgeQueryBuilder`；
- [ ] 渗透 Workflow Profile；
- [ ] 告警研判 Workflow Profile；
- [ ] Scope Allowlist；
- [ ] 查询指纹；
- [ ] 每任务调用预算；
- [ ] 每次 Replan 最大补充检索次数。
- [ ] 每个 Workflow 的 Experience Profile；
- [ ] 经验候选抽取、结构化和脱敏策略；
- [ ] 经验使用反馈及证据等级策略；
- [ ] 禁止模型直接设置经验状态和可见性。

### 14.4 Prompt 与 State

- [ ] `kb_hits` 标记为不可信证据；
- [ ] 禁止执行文档中的指令；
- [ ] 禁止知识改变目标范围和 Tool Allowlist；
- [ ] 本地 `chk-*` 才允许进入 `context_chunk_refs`；
- [ ] 保存 Source Ref 到 Checkpoint；
- [ ] Trace 记录 MCP 请求和使用情况；
- [ ] 保存命中来源类型和经验 ID，便于后续反馈；
- [ ] 经验候选与任务结果在同一事务写入 Outbox；
- [ ] 决策上下文继续执行 Token Budget。

## 15. Redis 的位置

MCP 多 Workflow 接入后，Redis 更有价值，但必须在 `content_revision` 之后实现。

推荐用途：

1. 查询规划共享缓存；
2. Query Embedding 缓存；
3. Search Result 缓存；
4. 相同请求并发合并；
5. Client/Scope/Workspace 限流；
6. 短期 Circuit Breaker 状态。
7. 短期事件去重和消费锁。

缓存键至少包含：

```text
scope
+ content_revision
+ normalized_query_hash
+ effective_parameters
+ filters
+ embedding_profile
+ reranker_version
```

Redis 不保存：

- Agent 业务状态；
- Worker 可靠任务；
- MCP 授权的唯一权威状态；
- 最终报告唯一副本；
- 文档或 Chunk 权威数据。
- 经验及其生命周期的权威状态；
- Feedback 幂等性的唯一保障。

Redis 故障必须 fail-open，不得让 MCP 和 RAG 全部不可用。
经验幂等的最终保障仍是数据库唯一约束；Redis 只能减少重复处理，不能替代权威持久化。

## 16. 韧性与错误语义

### 16.1 Agent Client

建议默认值：

```text
connect timeout       0.5～1.0 s
total tool timeout    2.5～3.0 s
network/429/503 retry 1 次，带 jitter
4xx retry             0 次
circuit breaker       连续失败后短时打开
failure policy        由 KnowledgePolicy.dependency 决定
```

具体值通过部署环境评测确定。

### 16.2 错误分类

```text
AUTH_REQUIRED
AUTH_FORBIDDEN
INVALID_ARGUMENT
UNKNOWN_SCOPE
RAG_UNAVAILABLE
RAG_TIMEOUT
RAG_DEGRADED
RESOURCE_NOT_FOUND
RESOURCE_STALE
SCHEMA_MISMATCH
RATE_LIMITED
INTERNAL_ERROR
```

经验写入 REST/事件链路另外定义：

```text
EXPERIENCE_INVALID
EXPERIENCE_CONFLICT
EXPERIENCE_FORBIDDEN
EXPERIENCE_STALE_REVISION
FEEDBACK_DUPLICATE
INDEX_PENDING
```

行为：

- Auth 错误中止知识调用并告警；
- Backend 超时/不可用时，`optional` 知识允许 Agent 继续运行；
- `required` 知识不可用时拒答、延后或转人工，不把“未知”表述成“安全”或“已完成”；
- `safety-critical` 知识不可用或 Coverage 不足时禁止执行依赖该知识的高风险动作；
- Schema Mismatch 触发熔断，避免错误数据进入 Plan；
- Degraded 且有可信结果时允许使用并记录；
- Resource Stale 重新搜索，不使用旧来源冒充当前证据。
- 重复 Feedback 返回幂等成功，不重复累计；
- 旧 `source_revision` 返回冲突并保留新版本；
- 索引暂未完成时经验不可进入正常检索，由补偿任务重试。

### 16.3 取消传播

Agent 任务暂停、取消或超时时：

- 取消 MCP Tool Call；
- MCP Gateway 取消内部 REST；
- RAG 取消多查询召回和 Rerank；
- 不继续创建 Agent Chunk；
- Trace 标记 cancelled，而不是 failed。

## 17. 可观测性

### 17.1 Trace

传播：

```text
traceparent
tracestate
X-Request-ID
X-Task-ID
X-Run-ID
X-Workflow-Type
X-Workspace-ID
X-Project-ID
```

身份和 Scope 权限仍来自 Token，不来自这些 Header。

MCP Context 提供 request ID；跨进程应使用 W3C Trace Context 关联：

- [OpenTelemetry Context Propagation](https://opentelemetry.io/docs/specs/otel/context/api-propagators/)

### 17.2 Metrics

RAG/MCP：

```text
mcp_requests_total{tool,workflow,status}
mcp_request_duration_seconds{tool,workflow}
mcp_auth_failures_total{reason}
mcp_search_hits{workflow,scope}
mcp_resource_reads_total{status}
rag_search_degraded_total{component}
rag_cache_hits_total{layer}
rag_experience_writes_total{workflow,status}
rag_experience_feedback_total{workflow,outcome}
rag_experience_transitions_total{workflow,from_status,to_status}
rag_experience_index_pending_total{workflow}
rag_experience_consumer_lag_seconds
```

Agent：

```text
knowledge_requests_total{workflow,trigger,status}
knowledge_request_duration_seconds{workflow}
knowledge_hits_materialized_total{workflow}
knowledge_hits_selected_total{workflow}
knowledge_chunk_ref_failures_total{reason}
knowledge_circuit_open_total
experience_candidates_total{workflow,status}
experience_feedback_events_total{workflow,outcome}
experience_outbox_pending_total{workflow}
```

禁止把 `task_id`、原始 Query、IP、Hash 等高基数字段放入 Metric Label。

### 17.3 Trace Event

建议事件：

```text
KNOWLEDGE_TRIGGERED
KNOWLEDGE_SKIPPED_CACHE
MCP_TOOL_CALLED
MCP_TOOL_DEGRADED
MCP_TOOL_FAILED
KNOWLEDGE_HIT_MATERIALIZED
KNOWLEDGE_HIT_SELECTED
KNOWLEDGE_RESOURCE_STALE
EXPERIENCE_CANDIDATE_EMITTED
EXPERIENCE_CANDIDATE_ACCEPTED
EXPERIENCE_FEEDBACK_EMITTED
EXPERIENCE_STATUS_CHANGED
```

## 18. 测试计划

### 18.1 RAG 单元测试

- MCP Input/Output Schema；
- Tool 参数边界；
- Scope → KB 映射；
- Token Claim 和 Scope 授权；
- Chunk Resource 隔离；
- 直接调用普通 REST 不能绕过 MCP Scope/Workspace 授权；
- Search 和 Resource Read 使用独立最小权限；
- 裸 `chunk_id` 跨知识库碰撞；
- `resource_ref` 防篡改和单来源定位；
- Scope 中无关知识库更新不会误判单个 Resource stale；
- content revision 递增；
- 跨库 RRF；
- degraded 合并；
-错误映射；
- Query 脱敏；
- Prompt Injection 文档；
- Redis fail-open；
- 经验状态机和默认检索状态；
- Upsert 幂等、迟到 `source_revision` 和并发更新；
- Feedback 去重和计数；
- 经验脱敏、Workspace 隔离和 Workflow 隔离；
- 双索引失败补偿；
- 经验降级、归档和索引移除。

### 18.2 MCP 协议测试

- initialize；
- capability negotiation；
- tools/list；
- tools/call；
- Structured Content 与 outputSchema；
- resources/templates/list；
- resources/read；
- protocol version header；
- stateless 多次调用；
- Origin/Host 校验；
- 401/403；
- Transport 授权错误与 Tool/Resource 业务授权错误语义；
- Cancellation；
- 并发调用；
- MCP Inspector 冒烟测试。

### 18.3 Agent 单元测试

- Trigger Gate；
- Query Builder；
- 查询指纹；
- MCP Client 超时和重试；
- Circuit Breaker；
- Structured Content Schema Mismatch；
- Resource 读取；
- external ID → `chk-*`；
- 本地 Chunk 幂等；
- tenant mismatch；
- Source Ref；
- optional 知识 fail-open，required/safety-critical 知识拒答或转人工；
- 每任务调用预算；
- 经验候选 Schema、脱敏和 Outbox 原子性；
- Experience 事件重放和发布重试；
- 经验使用反馈及证据等级；
- LLM 无法控制状态、Workspace 和可见性。

### 18.4 Contract Test

两个仓库共享固定 JSON Fixture：

```text
tests/contracts/knowledge_search_v1_request.json
tests/contracts/knowledge_search_v1_response.json
tests/contracts/knowledge_resource_v1.json
tests/contracts/knowledge_error_v1.json
tests/contracts/experience_upsert_v1.json
tests/contracts/experience_feedback_v1.json
tests/contracts/experience_event_v1.json
```

CI 同时校验：

- RAG 产出符合 Schema；
- Agent Client 能解析；
- 不允许静默删除必填字段；
- 新增字段必须向后兼容；
- 破坏性修改必须升级 `schema_version` 或 Tool 名。

### 18.5 端到端测试

渗透 Workflow：

- 新技术栈触发检索；
- CVE 精确检索；
- RAG 结果进入 Plan；
- `chk-*` 编译成功；
- RAG 不可用时任务继续；
- Prompt Injection 知识不能改变 Tool Allowlist。

告警研判 Workflow：

- 告警字段解释；
- IOC/CVE 查询；
- 攻击链综合；
- 误报条件；
- Playbook 推荐；
- 多知识库融合；
- 敏感日志脱敏；
- 跨 Workspace 隔离；
- 最终报告来源。

经验闭环：

- 渗透与告警 Workflow 的经验写入各自独立知识库；
- 候选经验默认不影响正常检索；
- 多次有效反馈后按策略晋级，随后可被检索；
- 失败反馈触发降权、降级或重新审核；
- 迟到事件和重复事件不破坏计数与状态；
- 私有经验不能跨 Workspace、Workflow 或 Service Identity 泄漏；
- Agent 旧经验库与 RAG Shadow 对比达到迁移阈值；
- RAG 写入或索引故障不影响 Workflow 主任务完成。

### 18.6 评测

继续使用现有网络安全 RAG 评测集，并新增 Agent 级评测：

| 指标 | 目标 |
|---|---|
| RAG Recall@10 | 不低于当前基线 |
| RAG nDCG | 不低于当前基线 |
| MCP 结构化响应成功率 | 100% |
| 非法跨 Scope 访问阻断率 | 100% |
| 外部 UUID 直接进入 Plan | 0 |
| 本地 Chunk 编译失败 | 0 |
| RAG 故障导致任务整体失败 | 0 |
| MCP 暖路径额外 P95 | 目标小于 100 ms，不含 RAG 检索 |
| Orchestrator 检索额外 P95 | 目标小于 1 s，结合当前检索基线校准 |
| 经验写入事件丢失率 | 0 |
| 经验重复事件重复计数率 | 0 |
| 未验证经验进入正常检索 | 0 |
| 跨 Workflow/Workspace 经验泄漏 | 0 |
| 经验检索质量 | 不低于 Agent 旧 Experience Store 基线 |

告警研判需要单独建设至少包含以下类别的数据集：

- 真阳性；
- 误报；
- 信息不足；
- 多告警关联；
- CVE 告警；
- Webshell；
- 凭证访问；
- 横向移动；
- 外联和 C2；
- 合规处置；
- Prompt Injection 日志。

## 19. 灰度与回滚

### 19.1 Feature Flags

RAG：

```text
RAG_MCP_ENABLED
RAG_MCP_AUTH_ENABLED
RAG_MCP_RESOURCES_ENABLED
RAG_SCOPE_FEDERATION_ENABLED
RAG_EXPERIENCE_ENABLED
RAG_EXPERIENCE_CONSUMER_ENABLED
RAG_EXPERIENCE_AUTO_PROMOTION_ENABLED
```

Agent：

```text
KNOWLEDGE_MCP_ENABLED
KNOWLEDGE_MCP_SHADOW_MODE
KNOWLEDGE_MCP_MATERIALIZE_ENABLED
KNOWLEDGE_MCP_INJECT_ENABLED
KNOWLEDGE_MCP_AGENTIC_TOOL_ENABLED
EXPERIENCE_RAG_PUBLISH_ENABLED
EXPERIENCE_RAG_FEEDBACK_ENABLED
EXPERIENCE_RAG_SHADOW_READ_ENABLED
EXPERIENCE_LEGACY_QDRANT_READ_ENABLED
```

### 19.2 灰度顺序

```text
1. MCP 服务上线，仅健康检查
2. Agent tools/list 与 Schema 校验
3. Shadow Search，只记录不注入
4. 对比 Agent 原静态 KB 与 RAG
5. Materialize，但不允许 Plan 引用
6. 少量任务允许 Plan 使用
7. 扩大渗透 Workflow
8. 上线经验模型和 Consumer，先接收 candidate
9. 新经验同步到 RAG，检索保持 Shadow
10. 对比旧 Experience Store 与 RAG 经验召回
11. 逐 Workflow 切换经验读取到 RAG
12. 接入告警研判 Workflow 及其独立经验库
13. 停止旧经验写入并逐步停止重复静态知识
14. 评估模型自主调用 Tool
```

### 19.3 回滚

任何阶段都可通过：

```text
KNOWLEDGE_MCP_INJECT_ENABLED=false
```

停止把 MCP 结果注入决策，而不影响：

- Agent 任务运行态、Checkpoint 和原始证据；
- Agent 原静态 KB；
- RAG REST；
- 文档入库；
- 前端。

MCP Gateway 独立部署，可单独回滚镜像。

经验迁移期还可以设置：

```text
EXPERIENCE_RAG_PUBLISH_ENABLED=false
EXPERIENCE_RAG_FEEDBACK_ENABLED=false
EXPERIENCE_LEGACY_QDRANT_READ_ENABLED=true
```

恢复旧经验读取。只有在完成数据回填校验、观察窗口和旧库备份后才能永久移除该回滚路径；RAG 中已持久化的经验事件不做破坏性删除。

## 20. 分阶段实施计划

### Phase 0：ADR 与契约冻结

- [x] 确认 REST Core + 独立 MCP Gateway 架构；
- [x] 确认 Tool/Resource 命名；
- [x] 确认 Scope 列表；
- [x] 确认每个 Workflow 的静态库、经验库和共享经验库拓扑；
- [x] 冻结任务态与长期经验的职责边界；
- [x] 确认鉴权方式；
- [x] 定义 Search/Resource/Error Schema；
- [x] 定义 Experience/Feedback/Event Schema 和生命周期；
- [x] 建立 Contract Fixture；
- [x] 定义 Feature Flag；
- [x] 记录当前 RAG 和 Agent 基线。

完成条件：

- 双方仓库能够针对同一 Fixture 通过 Contract Test；
- v1 的 Chunk 本地化、经验归属和基础 Scope 隔离决策已记录；
- 后续发现的内部 Search 绕过、Resource 身份和联邦业务归属问题进入 Phase 2.1，不回写为 Phase 0 已完成能力。

Phase 0 的权威交付物为 ADR、`contracts/v1/manifest.json`、JSON Schema 和
`tests/contracts/v1/` Fixture。本仓库已通过 Contract Test；Agent 仓库接入同一
Fixture 后，Phase 3 开始前还需在 Agent CI 中启用对应消费者测试。

### Phase 1：RAG 基础契约和版本

- [x] 实现 `content_revision`；
- [x] 实现稳定 Search Schema；
- [x] 实现结构化错误；
- [x] 实现按 KB 和 Chunk 精确读取；
- [x] 实现内部 REST Service Auth；
- [x] 补齐迁移和回滚测试。

完成条件：

- 文档生命周期正确递增 revision；
- 不能跨知识库读取 Chunk；
- 现有 RAG 评测无回归。

Phase 1 实现说明：

- `knowledge_bases.content_revision` 在文档进入或离开 `ready` 可检索集合时，
  与状态更新在同一数据库事务内原子递增；
- 旧数据库通过幂等增量迁移补充 `content_revision INTEGER NOT NULL DEFAULT 0`；
- `POST /v1/search` 增加 `trustguard-search-v1`、`request_id`、
  `content_revision`、结构化 `query_plan` 和枚举化 `coverage`；
- v1 HTTP 错误使用 `trustguard-error-v1` 信封，并暂时保留兼容字段 `detail`；
- `GET /v1/internal/knowledge-bases/{knowledge_base_id}/chunks/{chunk_id}`
  只允许携带 `RAG_INTERNAL_SERVICE_TOKEN` 的 Bearer 调用，且只读取匹配知识库中
  `ready` 文档的 `active` Chunk；
- 跨库访问和不存在统一返回 404，避免通过错误差异枚举 Chunk 归属；
- `tests/test_phase1_contract.py` 覆盖迁移幂等、发布/删除/失败路径、请求 ID、
  错误信封、鉴权以及跨库和未发布内容隔离；
- Phase 1 完成时全量测试为 295 项通过，检索评测测试无回归。

### Phase 2：只读 MCP Gateway

- [x] 引入并锁定 MCP Python SDK；
- [x] 实现 Streamable HTTP Stateless Server；
- [x] 实现 `knowledge_search`；
- [x] 实现 Resource Template；
- [x] 实现 OAuth Client Credentials Access Token 验证；
- [x] 实现 Scope Mapping；
- [x] 实现跨库 RRF；
- [x] 实现健康检查和 Metrics；
- [x] 完成 MCP 协议测试。

完成条件：

- 官方 Client 和 Inspector 均可调用；
- MCP Transport 鉴权和已实现的 Scope 隔离测试 100% 通过；
- MCP 额外开销达到目标；
- 关闭 MCP 不影响 REST。

该完成条件只描述 Phase 2 协议实现，不表示普通 REST 绕过、Workspace ABAC、
Resource 跨库身份和业务逻辑下沉已经完成；这些内容由 Phase 2.1 负责。

Phase 2 实现说明：

- 官方 `mcp==1.27.2` 被精确锁定在 `pyproject.toml` 和 `uv.lock`，避免 v2 稳定版发布
  后发生无意升级；
- `app/mcp_server/` 提供独立 Uvicorn 入口，采用
  `stateless_http=true`、`json_response=true` 和 `/mcp` 单端点；
- `RAG_MCP_SCOPE_MAPPING_JSON` 只接受冻结契约中的逻辑 Scope，可将一个 Scope 映射到
  多个知识库并限制内容类型；
- `knowledge_search` 并发调用各知识库，对库内排名执行跨库 RRF，任一知识库失败时保留
  可信结果并标记 `federation` 降级；
- 多库版本使用排序后的 `knowledge_base_id:content_revision` 计算 SHA-256；Chunk
  Resource 强制校验 Scope、知识库归属、当前 revision 以及内部 REST Service Token；
- Gateway 对查询中的常见凭证赋值和 Bearer Token 二次脱敏，Tool 标记为只读、幂等、
  非破坏且非开放世界；
- 生产鉴权通过 JWKS 验证短期 JWT 的签名、issuer、audience、expiry、OAuth scope 和
  `knowledge_scopes`，Origin 与 Host 由 MCP Transport Security 校验；
- `/health/live`、`/health/ready` 和 `/metrics` 与 REST 生命周期独立；
- 自动化测试使用官方 Python `ClientSession` 完成初始化、Tool 发现、Structured
  Content 调用和 Resource 回读，并使用冻结 JSON Schema 校验输出；
- 已使用官方 Inspector CLI 对真实 Streamable HTTP 进程执行 `tools/list` 冒烟，
  Tool annotations 与 Input/Output Schema 均成功返回。

### Phase 2.1：安全与业务边界加固

- [ ] 新增受服务身份保护的内部 Knowledge Search 接口；
- [ ] MCP 不再通过无服务鉴权的普通 `/v1/search` 检索；
- [ ] 将 Scope 映射、Workspace ABAC、联邦检索、配额、RRF、去重和 degraded 合并下沉到 Knowledge Application Service；
- [ ] 普通 REST、MCP 和评测复用相同业务服务；
- [ ] Search 和 Resource Read 实施独立最小权限；
- [ ] 明确 HTTP 401/403 与 MCP Tool/Resource 授权错误边界；
- [ ] 增加不透明 `resource_ref`、来源级 `source_revision/content_hash`；
- [ ] Resource 直接定位单个知识库和 Chunk，不遍历 Scope 后取第一个匹配；
- [ ] 联邦物理身份改为 `(knowledge_base_id, chunk_id)`；
- [ ] 保留旧 Resource URI 兼容读取并制定灰度退出计划；
- [ ] 生产模式下鉴权或内部服务身份缺失时启动失败；
- [ ] 生产部署不直接暴露 RAG 内部检索端口；
- [ ] 增加 REST 绕过、Workspace 越权、Chunk ID 碰撞和无关 revision 更新测试。

完成条件：

- 任何调用方都不能绕过 MCP 直接访问未授权知识库或 Workspace 私有内容；
- MCP、REST 和评测对相同请求使用同一检索语义；
- Search-only Token 不需要 `rag.resource.read`；
- Resource Read 能唯一定位来源，且不会因无关知识库更新失效；
- Phase 2 已有 Client 和旧 URI 在迁移窗口内保持兼容；
- 安全和边界加固测试全部通过。

Phase 2.1 是 Phase 3 的前置门槛。公共静态知识可继续使用 Phase 2 实现做开发验证，但
Workspace 私有知识、经验知识和生产 Agent 接入不得绕过本阶段。

### Phase 3：Agent 共享 Knowledge Gateway

- [ ] 拆分任务态、静态知识检索和长期经验职责；
- [ ] 建立协议无关的 `KnowledgeGateway` 和可替换 Transport Adapter；
- [ ] 实现 MCP Client；
- [ ] 实现 KnowledgePolicy；
- [ ] 实现 Trigger 和 Query Builder；
- [ ] 实现 Chunk Materializer；
- [ ] 实现 Source Ref；
- [ ] 实现 Retry、Timeout、Circuit Breaker；
- [ ] 实现 Trace 和 Metrics。

完成条件：

- MCP 不可用时 Agent 继续运行；
- optional/required/safety-critical 三类失败策略测试通过；
- RAG UUID 不会直接进入 Plan；
- Materialized Chunk 可被 Compiler 读取；
- 跨 Workspace 测试通过。

### Phase 4：渗透 Workflow 灰度

- [ ] Shadow Search；
- [ ] 与 Agent 原静态 KB 对比；
- [ ] 注入少量任务；
- [ ] 运行渗透 Workflow 测试集；
- [ ] 评估 Plan 成功率和延迟；
- [ ] 调整触发策略和预算；
- [ ] 逐步扩大。

完成条件：

- Plan 成功率不下降；
- 无新增越权；
- Chunk 编译失败为 0；
- 评测质量不低于基线。

### Phase 5：RAG 经验平台与可靠写入

- [ ] 实现经验数据模型、状态机和审计历史；
- [ ] 实现 Upsert、Feedback、Status 内部 REST；
- [ ] 实现 Service Scope、幂等和 `source_revision`；
- [ ] 实现 Agent Transactional Outbox；
- [ ] 实现 RabbitMQ Consumer、重试和死信队列；
- [ ] 实现检索文本投影与双索引补偿；
- [ ] 实现 Workspace/Workflow 强制隔离；
- [ ] 上线 `candidate/pending` Shadow 链路。

完成条件：

- Agent 主事务成功的经验事件不会丢失；
- 重放不会产生重复经验或重复计数；
- 未验证经验不会进入正常检索；
- 数据库与双索引最终一致且可审计；
- LLM 无经验发布和状态提升权限。

### Phase 6：渗透经验迁移

- [ ] 新经验同步至 `penetration-experience`；
- [ ] 回填并结构化 Agent 历史 Experience；
- [ ] 对比旧库与 RAG 的召回、决策贡献和延迟；
- [ ] 接入经验使用反馈；
- [ ] 按策略晋级和降级；
- [ ] 逐步切换经验读取；
- [ ] 停止旧库写入并保留回滚窗口。

完成条件：

- 经验评测不低于旧 Experience Store 基线；
- 新旧结果差异可解释；
- 无 Workspace 泄漏；
- 关闭旧库读路径后 Workflow 正常运行；
- 迁移和回滚演练通过。

### Phase 7：告警研判 Workflow

- [ ] 定义 Alert State；
- [ ] 定义 Alert KnowledgePolicy；
- [ ] 建立 `alert-triage-knowledge` 和 `alert-triage-experience`；
- [ ] 实现告警实体抽取和脱敏；
- [ ] 建设告警评测集；
- [ ] 接入多 Scope 联邦检索；
- [ ] 实现研判引用；
- [ ] 实现故障降级；
- [ ] 完成红队安全测试。

完成条件：

- 敏感日志不会未经处理发送到 RAG；
- 真阳性、误报和证据不足路径可区分；
- 报告中的知识引用可追溯；
- RAG 故障不会阻断告警处理。

### Phase 8：缓存和限流

- [ ] Planner Redis 缓存；
- [ ] Query Embedding Redis 缓存；
- [ ] Revision-aware Search 缓存；
- [ ] 相同请求合并；
- [ ] Client/Scope/Workspace 限流；
- [ ] 缓存命中率和容量监控。

完成条件：

- 缓存前后检索结果契约一致；
- 内容更新后不会命中旧 revision；
- Redis 故障不影响核心功能。

### Phase 9：可选 Agentic Tool

- [ ] 引入 `langchain-mcp-adapters`；
- [ ] Tool Allowlist；
- [ ] Runtime Interceptor；
- [ ] 每轮最大调用次数；
- [ ] 防止检索循环；
- [ ] 人工可见的 Tool Trace；
- [ ] 与确定性预检索做 A/B 评测。

完成条件：

- 模型自主检索能带来明确收益；
- 调用次数、延迟和成本在预算内；
- 无工具递归或 Prompt Injection 越权。

## 21. 风险与控制

| 风险 | 控制 |
|---|---|
| 把 MCP 当作唯一接口导致平台能力退化 | 保留 REST Core |
| 只因“多个 Workflow”引入 MCP，收益不足以覆盖复杂度 | Agent 依赖协议无关 KnowledgeGateway；以跨 Runtime 互操作和模型 Tool 需求作为 MCP 价值判据 |
| MCP Gateway 演变为第二套 RAG 业务内核 | Scope、ABAC、联邦、RRF、去重和 Resource 解析下沉到共享 Knowledge Application Service |
| 直接 REST 绕过 MCP 鉴权和 Scope | 内部 Search 使用服务身份；外部 REST 执行同等 ABAC；生产不暴露内部检索端口 |
| MCP SDK 生命周期影响现有 FastAPI | 独立 `rag-mcp` 服务 |
| LLM 自由选择任意知识库 | Scope Alias + Token Claim |
| 外部 UUID 导致 Plan 编译失败 | Agent Chunk 本地化 |
| 多库 Score 不可直接比较 | 跨库 RRF |
| 裸 Chunk ID 在多库中碰撞或错误合并 | 物理身份使用 `(knowledge_base_id, chunk_id)`；内容去重使用 content_hash |
| Scope 任一库更新导致所有 Resource stale | Scope revision 只管理搜索快照；Resource 使用来源级 revision/content_hash |
| Resource URI 暴露或可篡改物理知识库身份 | 服务端签发不透明 `resource_ref` 并在读取时重新授权 |
| RAG 内容更新后使用旧证据 | scope content_revision + source_revision/content_hash |
| 原始告警泄露敏感数据 | 实体抽取、脱敏、长度限制 |
| 文档 Prompt Injection | 不可信内容标记、Policy 不可覆盖 |
| RAG 故障阻断 Workflow 或导致错误放行 | KnowledgePolicy 按 optional/required/safety-critical 选择 fail-open、拒答或转人工 |
| 每轮都检索导致成本和延迟增加 | Trigger Gate、指纹、任务级缓存 |
| MCP Tool 被递归调用 | MVP 确定性调用、调用预算 |
| 权限依赖普通 Header | OAuth Token Claim 为权威 |
| 多套静态知识重复和冲突 | 联邦灰度后逐步迁移 |
| 每个租户和 Workflow 都建库导致知识库爆炸 | Workflow 级逻辑库为主，仅强隔离租户物理分库 |
| 经验包含命令输出、凭证或告警隐私 | Agent 先结构化脱敏，RAG 再校验，原始证据不入经验正文 |
| LLM 生成错误经验并自我强化 | 只能提交 candidate；确定性阈值、独立任务反馈和人工审核后晋级 |
| 重复或乱序事件破坏经验统计 | Outbox、唯一 event_id、source_revision 和数据库幂等约束 |
| 数据库成功但双索引失败 | index_pending、补偿任务，索引完成前不可见 |
| Agent 与 RAG 双写形成冲突 | 迁移期指定 RAG 为长期权威，旧库只读兼容 |
| 静态知识与经验内容重复导致排序偏置 | 保留 source_type、分源配额、跨源 RRF 和去重 |
| 经验条件漂移或过期 | expires_at、失败反馈、定期重审和自动降级 |

## 22. 最终推荐顺序

```text
REST 契约和 content_revision
        ↓
只读、无状态、带鉴权的 rag-mcp
        ↓
内部 Knowledge Search 服务身份与统一 ABAC
        ↓
联邦检索下沉到 Knowledge Application Service
        ↓
不透明 Resource Ref 与来源级版本
        ↓
Agent 共享 KnowledgeGateway
        ↓
RAG Chunk 本地化为 chk-*
        ↓
渗透 Workflow Shadow / 灰度
        ↓
RAG 原生经验模型、反馈和状态机
        ↓
Agent Outbox → RabbitMQ → RAG 可靠写入
        ↓
渗透经验 Shadow、回填和读取迁移
        ↓
告警研判 Workflow 与独立经验库接入
        ↓
Redis 缓存与限流
        ↓
可选的模型自主 MCP Tool
```

第一阶段的核心目标不是让模型“能看到一个新 Tool”，而是建立一个可被多个 Agent Runtime
安全复用、可追踪、可按风险降级、可验证引用的知识能力边界。MCP 是该边界的北向协议，
不是唯一业务内核或唯一安全边界。经验闭环则在只读检索、内部服务鉴权、Workspace ABAC
和 Resource 身份模型稳定后实施，确保“任务执行产生经验”和“经验参与未来决策”之间存在
脱敏、验证、反馈及审计边界。

## 23. 参考资料

- [MCP 2025-06-18 Specification](https://modelcontextprotocol.io/specification/2025-06-18)
- [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
- [MCP Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP Resource Schema](https://modelcontextprotocol.io/specification/2025-06-18/schema)
- [MCP OAuth Client Credentials](https://modelcontextprotocol.io/extensions/auth/oauth-client-credentials)
- [MCP Official Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [LangChain MCP Adapters](https://docs.langchain.com/oss/python/langchain/mcp)
- [LangChain Retrieval Architectures](https://docs.langchain.com/oss/python/langchain/retrieval)
- [OpenTelemetry Context Propagation](https://opentelemetry.io/docs/specs/otel/context/api-propagators/)
- [trustguard-agent KB Client](https://github.com/hdu-sangfor/trustguard-agent/blob/main/orchestrator/app/clients/kb_client.py)
- [trustguard-agent Chunk Store](https://github.com/hdu-sangfor/trustguard-agent/blob/main/orchestrator/app/core/chunk_store.py)
- [trustguard-agent RAG Plan Chunk Refs](https://github.com/hdu-sangfor/trustguard-agent/blob/main/orchestrator/app/core/rag_plan_chunk_refs.py)
