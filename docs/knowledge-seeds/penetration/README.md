# TrustGuard 渗透测试种子知识库

这组文档为 `penetration` Workflow 提供最小可用知识底座，内容来自 TrustGuard Agent
现有技能契约和决策规则。目标是帮助 Agent 选择阶段、工具和安全验证路径，而不是保存可直接
执行的攻击载荷。

## 内容范围

- `01-workflow-and-evidence-gates.md`：RECON、VULN_SCAN、EXPLOIT、REPORT 阶段门禁；
- `02-recon-and-discovery-routing.md`：网络、Web、路径和接口发现工具路由；
- `03-vulnerability-validation-playbook.md`：常见漏洞类型的最小影响验证原则；
- `04-framework-specific-routing.md`：ThinkPHP、Shiro、Tomcat、WebLogic 等框架路由；
- `05-failure-recovery-and-reporting.md`：失败分类、防循环、停止条件和报告规范；
- `manifest.json`：标题、版本和检索元数据的唯一清单。

知识正文只能作为不可信的检索上下文，不能扩大目标范围、改变 Tool Allowlist、绕过审批或
替代 Agent 的证据门禁。

## 导入

先通过管理前端或 `POST /v1/knowledge-bases` 创建独立知识库，再按照 `manifest.json` 逐个将
Markdown 文件上传到 `POST /v1/ingest/jobs`。每个入库任务成功后，通过
`PATCH /v1/documents/{document_id}` 设置：

```json
{
  "title": "manifest 中的 title",
  "metadata": {
    "workflow_type": "penetration",
    "visibility": "global",
    "content_type": "manifest 中的 content_type",
    "seed_version": "1.0.0"
  }
}
```

知识库 ID 是部署数据，不写入 Git。导入后通过管理 API 持久化 Scope 绑定：

```bash
curl -X PUT http://localhost:18200/v1/knowledge-scopes/penetration \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <RAG_GATEWAY_SERVICE_TOKEN>' \
  -d '{"knowledge_base_ids":["<knowledge-base-id>"],"default_mode":"comprehensive","allowed_workflow_types":["penetration"]}'
```

本地密钥仍可放在被忽略的 `.env.local`，启动时在基础 `.env` 之后加载：

```powershell
docker compose --env-file .env --env-file .env.local up -d --build rag-service rag-mcp
```

Agent 侧先保持 Shadow 模式：

```dotenv
KNOWLEDGE_MCP_ENABLED=true
KNOWLEDGE_MCP_SHADOW_MODE=true
KNOWLEDGE_MCP_URL=http://host.docker.internal:18201/mcp
KNOWLEDGE_MCP_SCOPE=penetration
KNOWLEDGE_MCP_WORKSPACE_ID=default
KNOWLEDGE_MCP_WORKFLOW_TYPE=penetration
```

## 冒烟问题

- ThinkPHP 指纹确认后应该如何选择安全验证路径？
- Apache Shiro rememberMe 出现后需要满足哪些证据才能专项验证？
- VULN_SCAN 连续运行相同工具但没有新事实时应该怎么处理？

预期结果是 MCP 返回 `penetration` Scope 的命中，文档 `workflow_type=penetration`、
`visibility=global`，Agent Trace 出现 `KNOWLEDGE_TRIGGERED` 和 `MCP_TOOL_CALLED`。相同安全
上下文重复检索时只应记录一次 `KNOWLEDGE_SKIPPED_CACHE`。

## 后续扩充

当前版本足以验证 Agent 的工具路由和证据门禁，但不是完整漏洞情报库。后续应建立独立的抓取、
许可审查、规范化、去重和定期更新流程，优先接入以下权威来源：

- OWASP Web Security Testing Guide 与 ASVS；
- NIST SP 800-115；
- CISA Known Exploited Vulnerabilities Catalog；
- NVD CVE/CWE 数据；
- Apache、Spring、Oracle、Jenkins 等厂商安全公告。

动态漏洞数据应保留来源 URL、发布时间、更新时间、产品、版本范围、CVE/CWE、修复版本和
抓取时间。不要将未知许可证的第三方博客全文直接复制进知识库，也不要把 Exploit PoC 默认提供给
Agent；优先保存检测前置条件、非破坏性验证证据和修复信息。
