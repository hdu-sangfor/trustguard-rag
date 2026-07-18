# 检索测评：hard-vector-only

- 时间：`2026-07-18T01:07:14+08:00`
- 数据集：`evaluation\cybersecurity\datasets-v02-hard\cybersecurity-dev.jsonl`
- 可回答问题：12
- 不可回答问题：0（当前只记录，不纳入检索相关性指标）
- 配置：向量=True，关键词=False，Rerank=False，融合=rrf

## 汇总指标

| 指标 | 数值 |
| --- | ---: |
| hit@1 | 0.9167 |
| hit@3 | 1.0000 |
| hit@5 | 1.0000 |
| hit@10 | 1.0000 |
| recall@10 | 1.0000 |
| mrr | 0.9444 |
| ndcg@10 | 0.9472 |
| 平均延迟 | 274.3 ms |
| P95 延迟 | 299.0 ms |
| 降级请求 | 0 |

## 未命中问题（Top 10）

无。

## Top 1 未命中问题

- `HARD-007` 58644 这个漏洞的 KEV 加入日和弱点类型是什么？ → 05-网络安全高混淆对照手册.pdf#page=8
