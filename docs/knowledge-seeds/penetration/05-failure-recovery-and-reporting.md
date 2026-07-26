# 渗透任务失败回退、停止条件与报告规范

## 故障分类

### 目标侧结果

- `not_vulnerable`：已有充分证据排除当前漏洞假设；
- `candidate`：存在指纹或弱信号，但证据不足；
- `confirmed`：满足确认门禁；
- `blocked`：认证、WAF、网络隔离或范围限制阻止验证。

### 工具与平台结果

- `timeout`：达到时间预算，不代表目标安全；
- `unavailable`：容器、依赖服务或网络不可用；
- `invalid_input`：目标、参数、run_id、chunk 或框架路由不完整；
- `degraded`：部分数据源或检索组件失败，但仍有可用结果；
- `cancelled`：任务被停止，应停止继续创建扫描和知识调用。

## 回退策略

1. 工具输入错误：修正结构化参数，不使用自由文本拼接危险参数。
2. 单一工具超时：降低范围、并发或深度，必要时选择互补工具。
3. 指纹冲突：回到 HTTP 响应、静态资源、错误栈和版本证据。
4. 专项模板无结果：确认入口和适用版本，不自动切换到更激进模式。
5. RAG/MCP 不可用：渗透 Workflow 继续运行并记录 `MCP_TOOL_FAILED`，不阻断任务。
6. RAG 返回降级结果：允许作为 Shadow 对比，不提升漏洞置信度。

## 防疲劳规则

当相同阶段、Todo、技术指纹和漏洞上下文没有变化时，不应重复知识检索。同一工具和参数连续执行
且没有新事实时，应触发以下动作之一：

- 推进到下一阶段；
- 改变待验证假设；
- 选择能够提供独立证据的工具；
- 标记限制条件并进入报告。

已经确认漏洞后，不要继续在 VULN_SCAN 堆叠同类扫描；应进入最小影响 EXPLOIT 或 REPORT。

## Evidence 与 Trace

关键事件应可关联到 task、phase、todo、skill execution 和 artifact。RAG Shadow Search 应记录：

- `KNOWLEDGE_TRIGGERED`：为什么需要检索；
- `MCP_TOOL_CALLED`：Scope、命中数量、延迟和 Resource Ref；
- `KNOWLEDGE_SKIPPED_CACHE`：相同知识指纹已检索；
- `MCP_TOOL_DEGRADED`：RAG 部分组件降级；
- `MCP_TOOL_FAILED`：调用失败但主流程继续。

Shadow 阶段的 Resource Ref 不能直接进入 Plan，也不能伪装成 Agent 本地 `chk-*`。只有后续完成
Resource Read、内容校验和本地 Materialize 后，才允许受控地进入决策上下文。

## 报告质量检查

每个结论回答以下问题：

- 测试的是哪个授权资产和入口？
- 哪些是直接观测事实，哪些是推断？
- 使用了什么工具、规则或模板？
- 是否有可重复证据，是否排除了 WAF、缓存和统一错误页？
- 风险影响是否与实际权限和数据边界相符？
- 修复后如何复测？
- 哪些限制导致结论仍需人工确认？

报告不得包含长期有效凭据、无关业务数据、可复用 WebShell 或超出必要范围的攻击载荷。
