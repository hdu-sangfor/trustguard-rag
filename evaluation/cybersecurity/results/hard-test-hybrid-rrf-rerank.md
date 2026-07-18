# 检索测评：hard-test-hybrid-rrf-rerank

- 时间：`2026-07-18T01:06:36+08:00`
- 数据集：`evaluation\cybersecurity\datasets-v02-hard\cybersecurity-test-gold.jsonl`
- 可回答问题：11
- 不可回答问题：1（当前只记录，不纳入检索相关性指标）
- 配置：向量=True，关键词=True，Rerank=True，融合=rrf

## 汇总指标

| 指标 | 数值 |
| --- | ---: |
| hit@1 | 0.8182 |
| hit@3 | 1.0000 |
| hit@5 | 1.0000 |
| hit@10 | 1.0000 |
| recall@10 | 1.0000 |
| mrr | 0.9091 |
| ndcg@10 | 0.9386 |
| 平均延迟 | 573.1 ms |
| P95 延迟 | 629.7 ms |
| 降级请求 | 0 |

## 未命中问题（Top 10）

无。

## Top 1 未命中问题

- `HARD-008` 缺少认证的 SharePoint Server 漏洞是哪天进入 KEV？ → 02-零日与在野利用漏洞案例.pdf#page=3
- `HARD-012` 只影响 FortiSandbox、没有写 Cloud 和 PaaS 的是哪条记录？ → 05-网络安全高混淆对照手册.pdf#page=6
