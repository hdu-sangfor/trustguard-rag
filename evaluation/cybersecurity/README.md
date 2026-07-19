# TrustGuard 网络安全 RAG 评测集

该目录维护一套统一的网络安全检索与回答评测集。语料快照日期为 `2026-07-18`，内容仅
覆盖防御性知识，不包含 PoC、武器化代码或未证实归因。

## 目录约定

```text
evaluation/cybersecurity/
├── source.json            # 应提交：人工审核的语料、问题和 Gold
├── dataset/               # 不提交：build_dataset.py 生成
├── results/               # 不提交：评测运行报告
├── build_dataset.py
├── run_retrieval_eval.py
├── run_answer_eval.py
└── README.md
```

`dataset/`、`results/`、PDF、临时 HTML 和 Python 缓存均为可再生成的本地产物，已由
`.gitignore` 排除。仓库只提交 `source.json`、构建与评测逻辑、文档和测试。

## 数据集规模

- 5 份文档、34 页、29 条唯一证据；
- 60 道问题，Dev/Test 各 30 道；
- 55 道可回答题、5 道不可回答题；
- 33 道题配置了额外等价或补充证据；
- 覆盖 CVE、时序辨析、否定条件、多证据问题、ATT&CK 相邻技术和安全运营推理。

`CYB-*` 和 `HARD-*` 是已经对外使用的稳定问题 ID。它们只表示历史命名，不再对应不同
数据集或目录；难度统一由 `difficulty` 字段表达。

主要字段：

- `query_id`：稳定问题 ID；
- `answerable`：当前快照下是否有足够证据；
- `evidence_ids` / `relevant_evidence`：原始必需证据，用于引用召回；
- `acceptable_evidence_ids` / `acceptable_evidence`：等价支持证据，用于引用精确率；
- `expected_answer` / `must_include`：参考答案和确定性必要事实。

测试盲测文件不包含可回答性、答案、必要事实或任何 Gold 字段。

## 构建

```powershell
uv run python evaluation/cybersecurity/build_dataset.py
```

构建器一次生成全部 5 份 PDF，并在 `dataset/` 中写入：

- `corpus-manifest.json`；
- `cybersecurity-dev.jsonl`；
- `cybersecurity-test-gold.jsonl`；
- `cybersecurity-test-queries.jsonl`；
- `stats.json`。

构建过程会检查文档名、证据 ID、问题 ID、可回答性、等价证据、PDF 页码和 SHA-256。
如果只修改问题或 Gold，可以使用 `--reuse-pdf` 复用输出目录中的 PDF：

```powershell
uv run python evaluation/cybersecurity/build_dataset.py --reuse-pdf
```

## 执行评测

两个执行器默认读取 `dataset/cybersecurity-dev.jsonl`，报告写入被忽略的 `results/`：

```powershell
uv run python evaluation/cybersecurity/run_retrieval_eval.py `
  --name retrieval-hybrid-rrf-rerank

uv run python evaluation/cybersecurity/run_answer_eval.py `
  --name answer-e2e
```

检索报告包含 Hit@k、Recall@k、MRR、nDCG 和延迟；回答报告包含回答/拒答准确率、必要
事实覆盖、引用精确率/召回率、延迟和 Token。`must_include` 只是确定性下界，正式结论仍应
结合语义裁判或人工抽检。

不要使用 Test Gold 反复调参。参数应在 Dev 集冻结后，再用
`cybersecurity-test-queries.jsonl` 执行盲测，并在结束后单独计分。

## 时效维护

涉及“最新”的事实只对 `2026-07-18` 快照负责。更新时应优先使用法规官网、CISA KEV、
厂商公告、NVD、MITRE ATT&CK 和 FIRST EPSS 等官方来源，同时提升版本号、重建 PDF 与
manifest，并在外部发布记录中保存模型、配置、语料哈希和评测时间。
