# ADR-0001：RAG MCP 接入与 Workflow 知识边界

- 状态：Accepted
- 日期：2026-07-24
- RAG 基线：`origin/main@93d08d0`
- Agent 调研基线：`trustguard-agent/main@c8f3796`
- 关联计划：[`../trustguard-agent-mcp-integration-plan.md`](../trustguard-agent-mcp-integration-plan.md)

## 背景

`trustguard-agent` 将从渗透测试扩展到告警研判等多个 Workflow。各 Workflow
需要复用静态知识和长期运行经验，但不能直接依赖 Qdrant、OpenSearch、Embedding
Profile、索引 Payload 或数据库表结构。

现有 Agent 还要求 Plan 中的上下文引用必须是任务本地 `chk-*`，因此外部 RAG
Chunk ID 不能直接进入 Plan。

## 决策

### 服务边界

- `trustguard-rag` 保留 REST Core，负责管理、入库、检索、评测以及后续经验写入。
- 新增独立、无状态的 `rag-mcp`，通过 Streamable HTTP `/mcp` 提供只读知识能力。
- MCP MVP 只提供 `knowledge_search` 和 Chunk Resource，不提供上传、删除或经验写入。
- Agent 通过共享 `KnowledgeGateway` 调用 MCP，并将外部 Chunk 本地化为 `chk-*`。

### 数据归属

| 数据 | 权威归属 |
|---|---|
| Todo、Plan、Checkpoint、原始输出、临时证据、`chk-*` | `trustguard-agent` |
| 法规、产品文档、CVE、安全手册、Playbook | `trustguard-rag` 静态知识库 |
| Workflow 长期经验 | `trustguard-rag` Workflow 独立经验库 |
| 经审核的跨 Workflow 通用经验 | `trustguard-rag` 共享经验库 |

任务记忆不等于长期经验。只有经过结构化、脱敏和可审计处理的经验候选才允许进入
RAG；LLM 无权直接发布经验或把经验提升为 `proven`。

### Scope 与知识库拓扑

第一批逻辑 Scope：

```text
penetration
alert-triage
compliance
product-docs
threat-intelligence
response-playbooks
```

Workflow Scope 的目标映射：

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

客户端只传逻辑 Scope，不能传任意知识库 ID。Scope 到知识库的映射及 Workspace
过滤由服务端根据已验证身份强制执行。

### 鉴权

- MCP 使用 OAuth 2.0 Client Credentials 和短期 Access Token。
- MCP 读权限：`rag.search`、`rag.resource.read`。
- 后续经验写权限：`rag.experience.write`、`rag.experience.feedback`。
- 经验管理权限：`rag.experience.admin`，不得授予 Workflow LLM。
- Token Claim 是授权权威；Workspace、Project、Task 等 Header 只用于路由和审计。

### 版本和兼容性

- 所有跨仓库消息都携带稳定的 `schema_version`。
- 只允许向后兼容地增加可选字段。
- 删除、改名、改变类型或语义必须发布新的 Schema 版本。
- `knowledge_search`、Resource、Error、Experience Upsert、Feedback 和 Event 的
  `v1` 契约由 `contracts/v1/` 下的 JSON Schema 冻结。

### 可靠性

- MCP 检索失败时 Agent 默认 fail-open。
- 后续经验生产写入采用 Agent Transactional Outbox → RabbitMQ → RAG Consumer。
- REST 经验写接口用于管理、补偿、回放和低流量调用。
- 事件使用唯一 `event_id`，经验使用 `(source_system, external_id)` 和
  `source_revision` 处理幂等与乱序。

## Feature Flags

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

## 后果

正向影响：

- 多个 Workflow 复用稳定知识契约，不感知索引实现。
- 长期经验具备统一生命周期、隔离、反馈和审计边界。
- MCP 可以独立灰度和回滚，不影响现有前端及 REST。

代价：

- 增加 MCP Gateway 和跨仓库 Contract Test。
- Agent 需要实现外部 Chunk 本地化。
- 经验迁移期间需要保留旧 Experience Store 的只读回滚路径。

## Phase 0 完成判定

- 本 ADR 为 Accepted；
- `contracts/v1/manifest.json` 列出的 Schema 和 Fixture 均存在；
- Contract Test 校验所有正例通过、所有反例被拒绝；
- 计划、ADR、Manifest 的基线和版本一致；
- 不包含数据库、API、MCP Server 或经验业务实现。
