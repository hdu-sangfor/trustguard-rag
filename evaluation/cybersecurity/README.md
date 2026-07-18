# TrustGuard 网络安全检索评测集

这是一个面向 TrustGuard 混合检索链路的可复现小型基准。第一版固定在
`2026-07-18` 快照，覆盖中国网络与数据安全监管政策、公开披露的在野利用漏洞、
MITRE ATT&CK v19、CVE 风险排序和安全运营案例。

第二版高混淆语料位于 `hard_source_data.json`、`datasets-v02-hard/` 和
`output/pdf/cybersecurity-eval-corpus-v02-hard/`，用于区分相邻 CVE、否定条件、相似日期和
ATT&CK 相邻技术；结果见 `results/hard-summary.md`。

语料仅包含防御侧需要的漏洞原理概括、影响、检测和缓解信息，不包含 PoC、利用请求、
武器化代码或对未公开攻击者的归因。

## 已生成内容

- `output/pdf/cybersecurity-eval-corpus/`：4 份待上传的 PDF，共 21 页。
- `source_data.json`：人工审核的语料、官方来源、问题和标准答案。
- `datasets/corpus-manifest.json`：PDF 哈希、页数、证据 ID 与页码映射。
- `datasets/cybersecurity-dev.jsonl`：18 条带完整标注的开发集。
- `datasets/cybersecurity-test-queries.jsonl`：18 条隐藏答案和证据标签的盲测问题。
- `datasets/cybersecurity-test-gold.jsonl`：与盲测问题对应的标准答案和证据标签。
- `datasets/stats.json`：类别、难度和可回答性分布。

当前共 36 条问题，其中 32 条可回答、4 条不可回答；包含精确 CVE、语义改写、
日期辨析、中英混合、相似 CVE 困难负样本、多证据问题和时效边界问题。

## 重新生成

本脚本只使用 Python 标准库，PDF 由 LibreOffice 生成，文本和页码由 Poppler 校验。
在当前 Windows 开发机上运行：

```powershell
.\.venv\Scripts\python.exe evaluation\cybersecurity\build_dataset.py
```

如程序不在默认位置，可以显式指定：

```powershell
.\.venv\Scripts\python.exe evaluation\cybersecurity\build_dataset.py `
  --libreoffice D:\software\LibreOffice\program\soffice.com `
  --pdftotext D:\software\texlive\2024\bin\windows\pdftotext.exe `
  --pdfinfo D:\software\texlive\2024\bin\windows\pdfinfo.exe
```

构建过程会执行以下一致性检查：

1. 文档名、证据 ID 和问题 ID 全局唯一；
2. 每条可回答问题至少关联一个真实证据 ID；
3. 每个证据 ID 在最终 PDF 中恰好出现一次；
4. PDF 不存在无文本空白页；
5. 生成后重新计算页码和 SHA-256，不使用人工填写的页码。

如果只修改问题或 gold 标注，不希望重新导出并改变已上传 PDF 的二进制哈希，可以复用现有文件：

```powershell
.\.venv\Scripts\python.exe evaluation\cybersecurity\build_dataset.py --reuse-pdf
```

## 使用方式

1. 清空或创建独立测试知识库，上传 4 份 PDF，等待状态全部变为 `ready`。
2. 使用开发集调节 `top_k`、候选池、融合方式和 Rerank 参数。
3. 参数冻结后只读取 `cybersecurity-test-queries.jsonl` 执行盲测。
4. 运行结束后再用 `cybersecurity-test-gold.jsonl` 计算指标。
5. 保存本次代码提交、配置、模型、语料哈希和评测时间，确保结果可复现。

不要用测试集反复调参；否则它会退化成第二个开发集。

## JSONL 关键字段

- `query_id`：稳定问题标识。
- `query`：发给 `/v1/search` 的查询文本。
- `category` / `difficulty`：问题类型和难度。
- `answerable`：当前快照的语料能否回答，仅存在于带标注文件。
- `evidence_ids`：标准相关证据，可多选。
- `relevant_evidence`：证据标题、PDF 文件名、页码、摘要和来源。
- `expected_answer`：以后评测 LLM 回答时使用的参考答案。
- `must_include`：答案一致性的必要事实，不建议单独作为最终评分。

## 检索指标

- `Hit@k`：前 k 条是否至少命中一个标准证据。
- `Recall@k`：前 k 条命中的标准证据数除以标准证据总数，适合多证据问题。
- `MRR@k`：第一个相关结果排名的倒数；越早出现越高。
- `nDCG@k`：评价相关证据的整体排序；当前可先采用二元相关性。
- `不可回答误召回率`：不可回答问题中，系统仍返回高置信相关证据的比例。

第一阶段不需要 LLM 回答模块，直接根据搜索结果携带的文档名、页码和证据 ID 计算这些指标。
后续接入回答模块后，再增加答案事实一致性、引用正确率、无证据断言率和拒答准确率。

## 执行基线测评

确认 4 份 PDF 已经入库并处于 `ready` 后运行：

```powershell
.\.venv\Scripts\python.exe evaluation\cybersecurity\run_retrieval_eval.py
```

脚本会调用真实 `/v1/search`，以 PDF 文件名和页码判断相关性，并将汇总报告与逐题搜索结果
写入 `evaluation/cybersecurity/results/`。也可以用参数执行消融实验：

```powershell
# 关闭 Rerank
.\.venv\Scripts\python.exe evaluation\cybersecurity\run_retrieval_eval.py `
  --name hybrid-rrf-no-rerank --no-enable-rerank

# 仅向量召回
.\.venv\Scripts\python.exe evaluation\cybersecurity\run_retrieval_eval.py `
  --name vector-only --no-enable-keyword --no-enable-rerank

# 仅 BM25 召回
.\.venv\Scripts\python.exe evaluation\cybersecurity\run_retrieval_eval.py `
  --name keyword-only --no-enable-vector --no-enable-rerank
```

## 时效和来源维护

涉及“最新”的事实只对 `2026-07-18` 快照负责。更新版本时应优先读取官方源：

- 中国政府网、全国人大和国家互联网信息办公室的法规原文；
- CISA KEV JSON、厂商 PSIRT/MSRC 和 NIST NVD；
- MITRE ATT&CK 官方版本页与技术页；
- FIRST EPSS 官方说明和带日期的数据。

更新后需要增加版本号、重新生成 PDF 和 manifest，并保留旧版结果用于纵向比较。
