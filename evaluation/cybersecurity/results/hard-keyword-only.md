# 检索测评：hard-keyword-only

- 时间：`2026-07-18T01:07:18+08:00`
- 数据集：`evaluation\cybersecurity\datasets-v02-hard\cybersecurity-dev.jsonl`
- 可回答问题：12
- 不可回答问题：0（当前只记录，不纳入检索相关性指标）
- 配置：向量=False，关键词=True，Rerank=False，融合=rrf

## 汇总指标

| 指标 | 数值 |
| --- | ---: |
| hit@1 | 0.7500 |
| hit@3 | 1.0000 |
| hit@5 | 1.0000 |
| hit@10 | 1.0000 |
| recall@10 | 0.9792 |
| mrr | 0.8611 |
| ndcg@10 | 0.8580 |
| 平均延迟 | 22.6 ms |
| P95 延迟 | 149.8 ms |
| 降级请求 | 0 |

## 未命中问题（Top 10）

无。

## Top 1 未命中问题

- `HARD-001` 哪个文件是 2025 年元旦开始执行，哪个要到 2026 年元旦？ → 05-网络安全高混淆对照手册.pdf#page=9
- `HARD-013` 为什么一个 2023 的 CVE 会在 2026 年的新增 KEV 里？ → 03-MITRE-ATTCK-v19-技战术图谱.pdf#page=2
- `HARD-015` 邮件到了和用户真的点开了，在 ATT&CK 里分别算什么？ → 03-MITRE-ATTCK-v19-技战术图谱.pdf#page=2
