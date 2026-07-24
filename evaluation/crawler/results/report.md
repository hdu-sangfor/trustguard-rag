# Crawler V2 数据集向量化模型评测

## 评测摘要

- 执行时间：`2026-07-24T11:44:21+08:00`
- 数据集版本：V2
- 原始数据：`F:\Project\trustguard\trustguard-crawler\doc`
- 数据指纹：`f3f03a943dd5449e95d3fa28f36809b4c5dfc1071d86471fdf2994681ef8b3f7`
- 语料：扫描 122 个源文件，入库 102 篇，排除 20 篇重复来源
- 查询：133 道；可回答 108，不可回答 25，显式编号 44
- 工作量：每个模型 102 次文档入库，266 次检索请求（纯向量 + 混合）
- 隔离性：每个模型使用独立的 V2 评测知识库，并用数据指纹阻止旧语料复用。

## 数据集设计

语料直接选自 `trustguard-crawler/doc` 的叶子 Markdown 与 Word 文档。聚合文件、索引、原始 API JSON 和报告文件不入库；同一安全编号及相同正文只保留一个规范文档。Word 文档在构建时转换成可追溯的 Markdown。

| 数据来源 | 入库文档数 |
|---|---:|
| CISA | 2 |
| China Standards | 6 |
| Chinese Regulation | 8 |
| MITRE CAPEC | 2 |
| MITRE CWE | 28 |
| NIST | 3 |
| NVD | 50 |
| OWASP | 3 |

| 查询类型 | 数量 |
|---|---:|
| `confusion` | 15 |
| `exact_lookup` | 20 |
| `semantic` | 73 |
| `unanswerable` | 25 |

评测集覆盖无编号语义检索、精确编号查询、相邻编号混淆和不可回答查询。相同问题对应多个等价来源时合并为一个 Gold 组，命中组内任意文档即可；同一文档的多个 chunk 不会重复得分。

## 检索参数

| 参数 | 值 | 作用 |
|---|---:|---|
| `repeats` | 1 | 完整重复轮数 |
| `top_k` | 10 | 最终结果数 |
| `vector_top_k` | 30 | 向量候选池 |
| `keyword_top_k` | 30 | 关键词候选池 |
| `fusion_method` | `weighted_score` | 混合检索融合方法 |
| `vector_weight` | 0.8 | 加权分数融合中的向量权重 |
| `keyword_weight` | 0.2 | 加权分数融合中的关键词权重 |
| `enable_rerank` | `false` | 关闭重排以观察 embedding 本身 |
| `max_chunks_per_document` | 1 | 文档级去重，避免同文档多 chunk 占满结果 |
| `api_failed_retries` | 2 | 仅 API profile 整个请求失败或向量降级后的额外重试次数 |
| `component_max_retries` | 2 | 服务端向量/关键词组件内部额外重试次数 |
| `enable_abstention` | `true` | 启用精确实体缺失和低向量置信度拒答 |

## 指标

- `Hit@1`：首条结果是否命中 Gold。
- `Recall@10`：前 10 条覆盖了多少个相关性需求；等价文档按一个需求计分。
- `MRR`：首个正确结果排名倒数的均值。
- `nDCG@10`：同时衡量相关结果是否命中以及是否靠前。
- `不可回答准确率`：不可回答题返回空结果的比例；误返回率与其互补。
- 质量指标只纳入 `search_status=ok` 且没有任何降级的成功请求。

## 严格成功请求结果

| 模型 | 维度 | 向量成功 | 请求重试恢复 | 组件内恢复 | Hit@1 | Recall@10 | MRR | nDCG@10 | 不可回答准确率 | P95 | 混合 nDCG@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `qwen3.7-text-embedding` | 1024 | 133/133 | 0 | 3 | 0.9352 | 1.0000 | 0.9645 | 0.9737 | 0.9600 | 305.2 ms | 0.9829 |
| `qwen3-embedding-0.6b` | 1024 | 133/133 | 0 | 0 | 0.9259 | 1.0000 |  0.9599 | 0.9702 | 1.0000 | 951.6 ms | 0.9829 |
| `qwen3.7-text-embedding-2560` | 2560 | 133/133 | 0 | 5 | 0.9167 | 1.0000 | 0.9568 | 0.9680 | 1.0000 | 372.6 ms | 0.9783 |
| `bge-m3` | 1024 | 133/133 | 0 | 0 | 0.8611 | 1.0000 | 0.9154 | 0.9365 | 0.9600 | 178.6 ms | 0.9549 |
| `text-embedding-v4` | 1024 | 133/133 | 0 | 3 | 0.8426 | 1.0000 | 0.9131 | 0.9352 | 1.0000 | 313.4 ms | 0.9450 |
| `text-embedding-v4-2048` | 2048 | 133/133 | 0 | 3 | 0.8333 | 1.0000 | 0.9078 | 0.9313 | 1.0000 | 340.4 ms | 0.9450 |

## 纯向量分层结果

| 模型 | 分层 | 成功数 | Recall@10 | nDCG@10 | 不可回答准确率 |
|---|---|---:|---:|---:|---:|
| `qwen3.7-text-embedding` | `exact_identifier` | 20 | 1.0000 | 0.9815 | 0.0000 |
| `qwen3.7-text-embedding` | `identifier_confusion` | 15 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3.7-text-embedding` | `semantic_ambiguous_gold` | 2 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3.7-text-embedding` | `semantic_without_identifier` | 71 | 1.0000 | 0.9651 | 0.0000 |
| `qwen3.7-text-embedding` | `unanswerable_identifier` | 15 | 0.0000 | 0.0000 | 1.0000 |
| `qwen3.7-text-embedding` | `unanswerable_semantic` | 10 | 0.0000 | 0.0000 | 0.9000 |
| `qwen3-embedding-0.6b` | `exact_identifier` | 20 | 1.0000 | 0.9815 | 0.0000 |
| `qwen3-embedding-0.6b` | `identifier_confusion` | 15 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-embedding-0.6b` | `semantic_ambiguous_gold` | 2 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3-embedding-0.6b` | `semantic_without_identifier` | 71 | 1.0000 | 0.9599 | 0.0000 |
| `qwen3-embedding-0.6b` | `unanswerable_identifier` | 15 | 0.0000 | 0.0000 | 1.0000 |
| `qwen3-embedding-0.6b` | `unanswerable_semantic` | 10 | 0.0000 | 0.0000 | 1.0000 |
| `qwen3.7-text-embedding-2560` | `exact_identifier` | 20 | 1.0000 | 0.9815 | 0.0000 |
| `qwen3.7-text-embedding-2560` | `identifier_confusion` | 15 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3.7-text-embedding-2560` | `semantic_ambiguous_gold` | 2 | 1.0000 | 1.0000 | 0.0000 |
| `qwen3.7-text-embedding-2560` | `semantic_without_identifier` | 71 | 1.0000 | 0.9566 | 0.0000 |
| `qwen3.7-text-embedding-2560` | `unanswerable_identifier` | 15 | 0.0000 | 0.0000 | 1.0000 |
| `qwen3.7-text-embedding-2560` | `unanswerable_semantic` | 10 | 0.0000 | 0.0000 | 1.0000 |
| `bge-m3` | `exact_identifier` | 20 | 1.0000 | 0.9815 | 0.0000 |
| `bge-m3` | `identifier_confusion` | 15 | 1.0000 | 1.0000 | 0.0000 |
| `bge-m3` | `semantic_ambiguous_gold` | 2 | 1.0000 | 1.0000 | 0.0000 |
| `bge-m3` | `semantic_without_identifier` | 71 | 1.0000 | 0.9086 | 0.0000 |
| `bge-m3` | `unanswerable_identifier` | 15 | 0.0000 | 0.0000 | 1.0000 |
| `bge-m3` | `unanswerable_semantic` | 10 | 0.0000 | 0.0000 | 0.9000 |
| `text-embedding-v4` | `exact_identifier` | 20 | 1.0000 | 0.9815 | 0.0000 |
| `text-embedding-v4` | `identifier_confusion` | 15 | 1.0000 | 0.9754 | 0.0000 |
| `text-embedding-v4` | `semantic_ambiguous_gold` | 2 | 1.0000 | 0.8155 | 0.0000 |
| `text-embedding-v4` | `semantic_without_identifier` | 71 | 1.0000 | 0.9170 | 0.0000 |
| `text-embedding-v4` | `unanswerable_identifier` | 15 | 0.0000 | 0.0000 | 1.0000 |
| `text-embedding-v4` | `unanswerable_semantic` | 10 | 0.0000 | 0.0000 | 1.0000 |
| `text-embedding-v4-2048` | `exact_identifier` | 20 | 1.0000 | 0.9631 | 0.0000 |
| `text-embedding-v4-2048` | `identifier_confusion` | 15 | 1.0000 | 0.9754 | 0.0000 |
| `text-embedding-v4-2048` | `semantic_ambiguous_gold` | 2 | 1.0000 | 0.8155 | 0.0000 |
| `text-embedding-v4-2048` | `semantic_without_identifier` | 71 | 1.0000 | 0.9163 | 0.0000 |
| `text-embedding-v4-2048` | `unanswerable_identifier` | 15 | 0.0000 | 0.0000 | 1.0000 |
| `text-embedding-v4-2048` | `unanswerable_semantic` | 10 | 0.0000 | 0.0000 | 1.0000 |

## 结论

- 本次严格成功请求的综合质量最高模型是 `qwen3.7-text-embedding`：纯向量 nDCG@10=0.9737，Recall@10=1.0000。
- 混合检索 nDCG@10 高于纯向量的模型：`qwen3.7-text-embedding`, `qwen3-embedding-0.6b`, `qwen3.7-text-embedding-2560`, `bge-m3`, `text-embedding-v4`, `text-embedding-v4-2048`。该结论只反映当前 `weighted_score` 参数（向量权重 0.8，关键词权重 0.2）。
- 不可回答准确率主要评价平台的拒答/阈值策略，不应单独用于判断 embedding 优劣。
- 自动生成集适合稳定回归和模型初筛；最终上线决策仍应补充人工审核的真实告警、漏洞处置和合规问答。
