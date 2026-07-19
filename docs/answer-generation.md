# 基于证据的回答层

回答层在现有混合检索之上提供单轮、非流式 RAG 问答，同时保留 `/v1/search` 作为独立、
稳定的检索接口。回答接口不会修改文档或索引。

## 处理流程

```text
POST /v1/answer
      │
      ├── HybridSearch：向量/BM25 → 融合 → 可选 Rerank
      │
      ├── ContextBuilder：去重 → 限制 Chunk 数 → Token 预算截断
      │
      ├── LLMClient：OpenAI-compatible Chat Completions
      │
      ├── JSON 契约解析
      │
      └── CitationValidator：正文引用 = 声明引用 = 真实证据
```

检索结果会被序列化为 JSON 证据数组。每条证据包含稳定的 `citation_id`、Chunk ID、文档
ID、来源 URI、文件名、页码和正文。系统提示词将证据正文视为不可信数据，明确禁止模型
执行证据中的指令。

## 配置

```env
RAG_LLM_PROVIDER=openai_compatible
RAG_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RAG_LLM_API_KEY=YOUR_LLM_API_KEY
RAG_LLM_MODEL=qwen-plus
RAG_LLM_TIMEOUT_SECONDS=60
RAG_LLM_TEMPERATURE=0.1
RAG_LLM_MAX_OUTPUT_TOKENS=1024
RAG_LLM_JSON_RESPONSE_FORMAT=true

RAG_ANSWER_CONTEXT_MAX_TOKENS=6000
RAG_ANSWER_MAX_CONTEXT_CHUNKS=8
RAG_ANSWER_REFUSAL_MESSAGE=当前知识库中没有足够证据回答该问题。
```

`RAG_LLM_PROVIDER` 默认是 `none`，因此原有入库和检索部署无需配置 LLM。未显式提供
`RAG_LLM_API_KEY` 时会回退读取 `DASHSCOPE_API_KEY`。如果兼容服务不支持
`response_format={"type":"json_object"}`，可设置 `RAG_LLM_JSON_RESPONSE_FORMAT=false`；
模型仍会被提示只输出 JSON，服务端继续严格解析。

上下文预算只约束检索证据，不包含系统提示词、问题和输出预算。当前使用与分块相同的
tokenizer 计数，以便在调用 LLM 前确定性地限制证据大小。

## API

### 请求

`POST /v1/answer` 使用与 `/v1/search` 相同的检索参数：

```json
{
  "query": "如何防御 SQL 注入？",
  "top_k": 10,
  "enable_vector": true,
  "enable_keyword": true,
  "enable_rerank": true,
  "filters": {
    "source_uri": "upload://security-guide.pdf"
  }
}
```

### 有证据的回答

```json
{
  "query": "如何防御 SQL 注入？",
  "status": "answered",
  "answer": "应使用参数化查询，并限制数据库账户权限。[1][2]",
  "citations": [
    {
      "citation_id": 1,
      "chunk_id": "chunk-1",
      "document_id": "doc-1",
      "source_uri": "upload://security-guide.pdf",
      "original_filename": "security-guide.pdf",
      "chunk_index": 4,
      "page_no": 12,
      "excerpt": "……"
    }
  ],
  "search_status": "ok",
  "effective_mode": "hybrid",
  "degraded_components": [],
  "retrieved_count": 10,
  "context_chunk_count": 8,
  "context_token_count": 5320,
  "retrieval_time_ms": 320.5,
  "generation_time_ms": 850.2,
  "total_time_ms": 1171.4,
  "model": "qwen-plus",
  "usage": {
    "prompt_tokens": 5500,
    "completion_tokens": 180,
    "total_tokens": 5680
  }
}
```

`citations` 只包含答案正文实际引用的证据，并按正文首次出现的顺序返回。模型也可以在
`insufficient_evidence` 回答中引用证据，用于解释现有资料覆盖了什么、仍缺少什么。

### 证据不足

检索结果为空时，服务不会调用 LLM：

```json
{
  "status": "insufficient_evidence",
  "answer": "当前知识库中没有足够证据回答该问题。",
  "citations": [],
  "generation_time_ms": 0.0
}
```

有候选片段但模型判断无法回答时，也返回 `insufficient_evidence`，并保留模型名称、生成
耗时、Token 用量以及正文实际使用的有效引用，便于区分两种拒答路径。

## 错误语义

| HTTP 状态 | 含义 |
|---|---|
| 400 | 向量和关键词检索被同时关闭 |
| 422 | 请求参数未通过 Schema 校验 |
| 502 | LLM 上游失败，或模型输出/引用不符合契约 |
| 503 | 所有启用的检索后端不可用，或 LLM 未配置 |
| 504 | LLM 调用超时 |

模型生成失败不会被转换成 `insufficient_evidence`，避免在评测中把格式故障或上游故障误算
为正确拒答。HTTP 错误不会包含上游响应正文或 API Key。

## 引用不变量

`answered` 响应必须同时满足：

1. 答案正文至少包含一个 `[n]` 引用；
2. 正文编号集合与模型 JSON 中的 `citation_ids` 集合相同；
3. 每个编号都对应本次实际提供给模型的证据。

如果模型已在结构化 `citation_ids` 中声明了有效证据、但偶发漏写全部或部分正文标记，服务
会按声明顺序确定性补齐 `[n]`。服务只会渲染本次实际提供给模型的证据编号，不会猜测或创造引用；
`citation_ids` 为空、编号越界，或正文编号与声明不一致时仍返回 502。该机制能验证引用编号
和来源映射，但无法仅靠规则判断一句话是否真的被引用内容支持；事实忠实度仍需通过自动
评测与人工抽检衡量。

## 当前边界

首版不包含多轮记忆、查询改写、联网搜索、流式 SSE、自动重试和用户/租户权限控制。
`trustguard-agent` 后续既可以调用 `/v1/search` 自行生成，也可以把 `/v1/answer` 当作独立
知识问答工具。
