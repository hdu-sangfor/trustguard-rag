# TrustGuard Knowledge Contract v1

本目录是 `trustguard-rag` 与 `trustguard-agent` 之间的版本化契约源。

规则：

- `manifest.json` 是 v1 契约清单；
- `schemas/` 保存 JSON Schema Draft 2020-12；
- `tests/contracts/v1/valid/` 保存必须通过的正例；
- `tests/contracts/v1/invalid/` 保存必须被拒绝的反例；
- 实现代码不得修改 Schema 来迁就单个测试；
- 向后兼容字段只能作为非必填字段增加；
- 破坏性修改必须新建 `contracts/v2/`。

v1 冻结以下边界：

| Contract | 方向 | 用途 |
|---|---|---|
| `knowledge_search_request` | Agent → MCP/RAG | 只读知识检索 |
| `knowledge_search_response` | MCP/RAG → Agent | 结构化命中、覆盖和降级信息 |
| `knowledge_resource` | MCP/RAG → Agent | 精确读取命中的完整 Chunk |
| `knowledge_error` | MCP/RAG → Agent | 稳定错误信封 |
| `experience_upsert` | Agent/Admin → RAG | 幂等写入经验候选 |
| `experience_feedback` | Agent → RAG | 提交经验使用效果 |
| `experience_event` | Agent Outbox → RAG Consumer | 可靠异步经验事件 |

MCP 只暴露前四项只读契约。后三项供后续内部 REST 和 RabbitMQ 写入链路使用。

Phase 2.1 为 `knowledge_search_response` 的 Hit 和 `knowledge_resource` 增加了向后兼容的可选
字段 `resource_ref`、`source_revision`、`content_hash`。新客户端优先通过
`trustguard-rag://{scope}/resources/{resource_ref}` 回读；旧 Chunk Resource URI 在迁移期保留。
