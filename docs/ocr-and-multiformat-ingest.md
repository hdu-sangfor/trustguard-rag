# 多格式入库与 OCR

TrustGuard RAG 支持本地上传多种文件格式，并对 PDF 内嵌图片 / 整图做 OCR。人工复核仅提供后端 API（无前端面板）。

## 支持格式

| 类型 | MIME / 扩展名 | 说明 |
|------|----------------|------|
| PDF | `application/pdf` | 默认 MinerU；可切换为文本层 + 图片区域裁剪 OCR |
| Word | `.docx` | MinerU 文档解析 |
| 纯文本 | `.txt` `.log` | UTF-8 / BOM / GBK |
| Markdown | `.md` | 剥离 YAML front matter（写入 metadata） |
| CSV / JSON / HTML | `.csv` `.json` `.html` | 转为可读纯文本后分块 |
| 图片 | `.png` `.jpg` `.webp` `.gif` `.bmp` `.tif` | 整图 OCR（需开启 OCR） |

本期不支持：旧式 `.doc`、其他 Office 格式、音视频、压缩包。

## OCR 配置

```bash
# none | local | api
RAG_OCR_PROVIDER=none

# api 时：bailian | openai_compatible | custom
RAG_OCR_API_DRIVER=openai_compatible
RAG_OCR_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
RAG_OCR_API_KEY=...
RAG_OCR_API_MODEL=qwen-vl-ocr

# 本地 Paddle（需 pip install '.[ocr-local]'）
# RAG_OCR_PROVIDER=local

# 空识别 / 单块失败默认 fail-open
RAG_OCR_FAIL_OPEN=true

# Custom HTTP OCR
# RAG_OCR_API_DRIVER=custom
# RAG_OCR_CUSTOM_BASE_URL=https://ocr.example.com
# RAG_OCR_CUSTOM_PATH=/ocr
# RAG_OCR_CUSTOM_REQUEST_TEMPLATE=multipart   # 或 base64_json
# RAG_OCR_CUSTOM_RESPONSE_JSONPATH=$.text
# 默认拒绝内网/回环 URL；本地联调再开：
# RAG_OCR_ALLOW_PRIVATE_URLS=true
```

Docker 默认不装 Paddle 大镜像；容器内用 `api` 或 `none` 即可。

## PDF 行为

默认 `RAG_PDF_PARSER=mineru`：上传前先校验 PDF、加密状态和页数上限，再由 MinerU
返回结构化内容，并恢复 `--- Page N ---` 页码标记供分块与引用使用。MinerU 的连接
失败、超时、HTTP 429 和 5xx 会进入任务重试。

显式设置 `RAG_PDF_PARSER=local` 时使用以下本地链路：

1. 每页抽取文本层。
2. 用 PyMuPDF 检测图片 bbox，**只渲染裁剪区**再 OCR（非整页）。
3. 合并页面文本 + OCR spans；空识别记 `status=empty`。
4. 单块失败时（`RAG_OCR_FAIL_OPEN=true`）记 `failed`，不阻断其它页。
5. 全文最终仍空 → `EMPTY_CONTENT`；OCR 区域仍会落库便于复核。

## 人工复核 API（无 UI）

- `GET /v1/documents/{id}/ocr-regions`
- `GET /v1/ocr-regions/{id}`
- `GET /v1/ocr-regions/{id}/image`
- `POST /v1/ocr-regions/{id}/review` body: `{ "action": "approve" | "correct", "corrected_text"?: "..." }`

`correct` 会保存人工文本，并从独立保存的非 OCR 文本基线确定性重建全文，再执行
chunk/embed/index。发布失败会尝试恢复旧分块与索引，不会把旧机器识别文字重复追加。

## 检索相关修复（同分支）

- `weighted_score` 融合前对两侧分数分别 min-max 归一化。
- 关键词检索路径不再同步触发 OpenSearch backfill（仅 startup / 运维入口）。
- Pseudo BM25 假分使用 `hashlib.sha256` 稳定种子。
