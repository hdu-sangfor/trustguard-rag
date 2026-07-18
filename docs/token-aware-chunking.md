# 基于 Qwen tokenizer 的中文分块

入库流程使用 `Qwen/Qwen3-Embedding-0.6B` 的本地 tokenizer 计算单个分块的
`token_count`，并通过 LangChain `RecursiveCharacterTextSplitter` 生成分块。

默认窗口：

- 目标长度：384 Token
- 重叠长度：64 Token
- 分隔顺序：段落、换行、中文句末标点、分号、逗号、空格、字符
- PDF 页码：分块不会跨页

`chunks.token_count` 是本地 tokenizer 得到的单分块长度，用于分块控制、展示和统计。
远程嵌入 API 返回的 `usage.prompt_tokens` 与 `usage.total_tokens` 是调用级准确用量，
只做汇总日志和迁移结果统计，不会平均分配到单个分块。

## 配置

```env
RAG_CHUNK_TOKENIZER_MODEL=Qwen/Qwen3-Embedding-0.6B
RAG_CHUNK_TARGET_TOKENS=384
RAG_CHUNK_OVERLAP_TOKENS=64
```

tokenizer 与本地嵌入模型共用 Hugging Face、ModelScope 和缓存目录配置。

## 迁移已有文档

修改分块模型或窗口后，仅重建搜索索引不能改变已有分块。请在停止 API 和 Worker
写入的维护窗口执行：

```bash
python -m app.core.indexing.rechunk_search_indexes
```

命令会先读取所有 `ready` 文档的 `extracted.txt`，完成新分块和向量计算；全部准备
成功后，再重建 Qdrant、OpenSearch，并在一个 MySQL 事务中替换旧分块。任一文档
缺少抽取产物时会在修改存储前失败。
