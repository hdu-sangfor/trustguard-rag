# trustguard-rag 动态测试报告

- **时间**: 2026-07-08（Docker live 复测）
- **模式**: **live**（Docker Compose 全栈）
- **目标**: http://127.0.0.1:18200
- **结果**: 12/12 通过

## 两种测试模式对比

| | in-process | Docker live |
|---|---|---|
| 运行位置 | 本机 Python 直接加载 FastAPI | 容器内 uvicorn |
| 数据库 | SQLite（测试用） | MySQL 8（真实 migration） |
| 对象存储 | **本地目录** `RAG_MINIO_ENABLED=false` | **MinIO** `RAG_MINIO_ENABLED=true` |
| Qdrant | mock（默认） | mock（默认） |
| 适用场景 | 快速回归、CI 无 Docker | 验收、存储/依赖真实行为 |

> **此前 in-process 报告未覆盖 MinIO**，你的判断正确。本次已在 Docker 下复测并单独验证 bucket 内对象。

## Docker 环境依赖状态

```
GET /health/ready → 200
mysql: up
minio: up
qdrant: disabled (mock mode)
```

## 测试范围

| 类别 | 用例 |
|------|------|
| 健康检查 | live / ready / mysql+minio |
| API 契约 | capabilities、multipart 上传 |
| 入库主路径 | PDF → job succeeded → document ready |
| 数据完整性 | chunks 含 page_no、artifacts 三件套 |
| MinIO 存储 | bucket `trustguard-rag-artifacts` 内存在 raw.pdf / extracted.txt / meta.json |
| 幂等 | 相同字节 deduplicated |
| 错误路径 | 损坏 PDF → CORRUPT_FILE |
| 冲突 | conflict + resolve → 旧 doc superseded |

## 明细（live）

| 状态 | 用例 | 说明 |
|------|------|------|
| PASS | health/live | alive |
| PASS | health (deps) | mysql+minio up, qdrant disabled |
| PASS | ingest PDF happy path | job succeeded |
| PASS | document ready | status=ready |
| PASS | chunks page_no | 每 chunk 有 page_no |
| PASS | artifacts list | raw.pdf, extracted.txt, meta.json |
| PASS | artifact download | 可下载 extracted.txt |
| PASS | deduplication | 同内容重复上传 |
| PASS | corrupt PDF | CORRUPT_FILE |
| PASS | filename conflict | status=conflict |
| PASS | conflict resolve | 新 doc ready，旧 superseded |

## MinIO 直验

对 document `3c19d19b-...` 列出 bucket 前缀 `artifacts/.../v1/`：

- `artifacts/{id}/v1/raw.pdf`
- `artifacts/{id}/v1/extracted.txt`
- `artifacts/{id}/v1/meta.json`

## 附：MySQL migration 修复

首次 Docker 启动失败原因：`source_uri VARCHAR(2048)` 参与 UNIQUE 索引超 MySQL 3072 字节上限。已改为 `source_uri(256)` 前缀索引。
