"""动态集成测试运行器。

用法：
  python tests/run_dynamic_tests.py              # 测试 127.0.0.1:18200 上运行的服务
  python tests/run_dynamic_tests.py --in-process # 使用 ASGI 应用和 SQLite，不依赖 Docker
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import fitz
import httpx

DEFAULT_BASE = "http://127.0.0.1:18200"
TIMEOUT = 60.0


@dataclass
class CaseResult:
    name: str
    passed: bool
    detail: str
    duration_ms: float


@dataclass
class TestReport:
    mode: str
    started_at: str
    base_url: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)


def make_pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def run_case(name: str, fn) -> CaseResult:
    t0 = time.perf_counter()
    try:
        detail = fn()
        passed = True
    except Exception as e:
        detail = str(e)
        passed = False
    return CaseResult(name, passed, detail, round((time.perf_counter() - t0) * 1000, 1))


def wait_job(client: httpx.Client, job_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/v1/ingest/jobs/{job_id}")
        r.raise_for_status()
        job = r.json()
        if job["status"] not in {
            "queued",
            "running",
            "resolving",
            "ingest_retrying",
            "resolve_retrying",
        }:
            return job
        time.sleep(0.15 if hasattr(client, "_transport") else 0.3)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


def execute_suite(client: httpx.Client, report: TestReport) -> None:
    state: dict = {}
    run_id = uuid.uuid4().hex[:10]

    def t_live():
        r = client.get("/health/live")
        assert r.status_code == 200
        assert r.json()["status"] == "alive"
        return "alive"

    def t_health():
        r = client.get("/health")
        r.raise_for_status()
        body = r.json()
        assert body["status"] == "ok", body
        deps = body["dependencies"]
        assert deps["mysql"]["status"] == "up"
        qdrant = deps.get("qdrant", {})
        assert qdrant.get("status") in {"disabled", "up"}
        storage = deps.get("minio") or deps.get("local_storage")
        assert storage["status"] in {"up", "disabled"}
        return json.dumps({k: v["status"] for k, v in deps.items()}, ensure_ascii=False)

    def t_capabilities():
        r = client.get("/v1/sources/capabilities")
        r.raise_for_status()
        data = r.json()
        mimes = set(data["sources"][0]["mime_types"])
        required = {
            "application/pdf",
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
            "text/html",
            "image/png",
        }
        missing = required - mimes
        assert not missing, f"missing mime types: {missing}"
        return f"mimes={len(mimes)} max_bytes={data['sources'][0]['max_bytes']}"

    def t_upload_pdf():
        pdf = make_pdf([f"Dynamic test page one {run_id}", f"Dynamic test page two {run_id}"])
        state["pdf_bytes"] = pdf
        fname = f"dynamic-report-{run_id}.pdf"
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (fname, pdf, "application/pdf")},
        )
        r.raise_for_status()
        job_id = r.json()["job_id"]
        job = wait_job(client, job_id, timeout=90)
        assert job["status"] == "succeeded", job
        state["document_id"] = job["document_id"]
        return f"job={job_id} doc={job['document_id']}"

    def t_document_ready():
        doc_id = state["document_id"]
        r = client.get(f"/v1/documents/{doc_id}")
        r.raise_for_status()
        doc = r.json()
        assert doc["status"] == "ready"
        return f"filename={doc['original_filename']}"

    def t_chunks_page_no():
        doc_id = state["document_id"]
        r = client.get(f"/v1/documents/{doc_id}/chunks")
        r.raise_for_status()
        chunks = r.json()
        assert len(chunks) >= 1
        assert all(c["page_no"] is not None for c in chunks)
        return f"chunks={len(chunks)}"

    def t_artifacts_list():
        doc_id = state["document_id"]
        r = client.get(f"/v1/documents/{doc_id}/artifacts")
        r.raise_for_status()
        files = r.json()["files"]
        assert {"raw.pdf", "extracted.txt", "meta.json"}.issubset(set(files))
        return ",".join(sorted(files))

    def t_artifact_download():
        doc_id = state["document_id"]
        r = client.get(f"/v1/documents/{doc_id}/artifacts/extracted.txt")
        r.raise_for_status()
        assert "--- Page 1 ---" in r.text
        return f"bytes={len(r.text)}"

    def t_dedup():
        pdf = state["pdf_bytes"]
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (f"other-name-{run_id}.pdf", pdf, "application/pdf")},
        )
        r.raise_for_status()
        job = wait_job(client, r.json()["job_id"], timeout=90)
        assert job["status"] == "deduplicated"
        return "ok"

    def t_corrupt_pdf():
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (f"bad-{run_id}.pdf", b"not-pdf", "application/pdf")},
        )
        r.raise_for_status()
        job = wait_job(client, r.json()["job_id"], timeout=90)
        assert job["status"] == "failed"
        assert job["error_code"] == "CORRUPT_FILE"
        return job["error_code"]

    def t_conflict():
        pdf_a = make_pdf([f"Conflict version A {run_id}"])
        pdf_b = make_pdf([f"Conflict version B totally different {run_id}"])
        name = f"conflict-same-{run_id}.pdf"
        j1 = wait_job(
            client,
            client.post(
                "/v1/ingest/jobs",
                data={"source_type": "file"},
                files={"file": (name, pdf_a, "application/pdf")},
            ).json()["job_id"],
            timeout=90,
        )
        assert j1["status"] == "succeeded"
        state["conflict_old"] = j1["document_id"]
        j2 = wait_job(
            client,
            client.post(
                "/v1/ingest/jobs",
                data={"source_type": "file"},
                files={"file": (name, pdf_b, "application/pdf")},
            ).json()["job_id"],
            timeout=90,
        )
        assert j2["status"] == "conflict"
        state["conflict_job"] = j2["id"]
        state["conflict_pending"] = j2["pending_document_id"]
        return f"pending={j2['pending_document_id']}"

    def t_resolve_conflict():
        r = client.post(
            f"/v1/ingest/jobs/{state['conflict_job']}/resolve",
            json={"keep_document_id": state["conflict_pending"]},
        )
        r.raise_for_status()
        job = wait_job(client, state["conflict_job"], timeout=90)
        assert job["status"] == "succeeded"
        old = client.get(f"/v1/documents/{state['conflict_old']}").json()
        assert old["status"] == "superseded"
        return f"new={job['document_id']}"

    def _ingest_ok(filename: str, data: bytes, mime: str) -> dict:
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (filename, data, mime)},
        )
        r.raise_for_status()
        job = wait_job(client, r.json()["job_id"], timeout=90)
        assert job["status"] == "succeeded", job
        return job

    def t_ingest_txt():
        body = f"hello dynamic txt 你好 {run_id}".encode("utf-8")
        job = _ingest_ok(f"note-{run_id}.txt", body, "text/plain")
        state["txt_doc"] = job["document_id"]
        text = client.get(f"/v1/documents/{job['document_id']}/artifacts/extracted.txt").text
        assert "hello dynamic txt" in text
        return f"doc={job['document_id']}"

    def t_ingest_md():
        raw = f"---\ntitle: t\n---\n# Heading {run_id}\nbody line".encode()
        job = _ingest_ok(f"note-{run_id}.md", raw, "text/markdown")
        text = client.get(f"/v1/documents/{job['document_id']}/artifacts/extracted.txt").text
        assert "Heading" in text
        return f"doc={job['document_id']}"

    def t_ingest_csv():
        job = _ingest_ok(f"data-{run_id}.csv", f"a,b\n1,{run_id}\n".encode(), "text/csv")
        text = client.get(f"/v1/documents/{job['document_id']}/artifacts/extracted.txt").text
        assert "a" in text and run_id in text
        return f"doc={job['document_id']}"

    def t_ingest_json():
        job = _ingest_ok(
            f"obj-{run_id}.json",
            json.dumps({"k": "v", "run": run_id}).encode(),
            "application/json",
        )
        text = client.get(f"/v1/documents/{job['document_id']}/artifacts/extracted.txt").text
        assert "k" in text
        return f"doc={job['document_id']}"

    def t_ingest_html():
        html = f"<html><script>bad()</script><body><p>HelloHTML {run_id}</p></body></html>".encode()
        job = _ingest_ok(f"page-{run_id}.html", html, "text/html")
        text = client.get(f"/v1/documents/{job['document_id']}/artifacts/extracted.txt").text
        assert "HelloHTML" in text
        assert "bad()" not in text
        return f"doc={job['document_id']}"

    def t_empty_txt_fails():
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (f"empty-{run_id}.txt", b"   \n", "text/plain")},
        )
        r.raise_for_status()
        job = wait_job(client, r.json()["job_id"], timeout=90)
        assert job["status"] == "failed"
        assert job["error_code"] == "EMPTY_CONTENT"
        return job["error_code"]

    def t_unsupported_mime():
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (f"x-{run_id}.bin", b"\x00\x01\x02unknown", "application/octet-stream")},
        )
        r.raise_for_status()
        job = wait_job(client, r.json()["job_id"], timeout=90)
        assert job["status"] == "failed"
        assert job["error_code"] in {"UNSUPPORTED_MIME", "CORRUPT_FILE"}
        return job["error_code"]

    def t_image_without_ocr():
        # 默认 OCR=none：图片入库应失败 OCR_UNAVAILABLE
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        r = client.post(
            "/v1/ingest/jobs",
            data={"source_type": "file"},
            files={"file": (f"tiny-{run_id}.png", png, "image/png")},
        )
        r.raise_for_status()
        job = wait_job(client, r.json()["job_id"], timeout=90)
        assert job["status"] == "failed", job
        assert job["error_code"] == "OCR_UNAVAILABLE", job
        return job["error_code"]

    def t_ocr_regions_list_empty_ok():
        # 对已有 PDF 文档列 OCR regions 应 200（可能为空列表）
        doc_id = state["document_id"]
        r = client.get(f"/v1/documents/{doc_id}/ocr-regions")
        r.raise_for_status()
        assert isinstance(r.json(), list)
        return f"count={len(r.json())}"

    def t_search_smoke():
        # 混合检索冒烟（依赖向量/关键词服务是否就绪）
        r = client.post(
            "/v1/search",
            json={"query": f"Dynamic test page {run_id}", "top_k": 5},
        )
        if r.status_code == 404:
            return "search endpoint absent (skip)"
        r.raise_for_status()
        body = r.json()
        assert "results" in body or "items" in body or isinstance(body, list)
        return f"status={r.status_code}"

    cases = [
        ("health/live", t_live),
        ("health (deps)", t_health),
        ("sources/capabilities", t_capabilities),
        ("ingest PDF happy path", t_upload_pdf),
        ("document ready", t_document_ready),
        ("chunks page_no", t_chunks_page_no),
        ("artifacts list", t_artifacts_list),
        ("artifact download", t_artifact_download),
        ("deduplication", t_dedup),
        ("corrupt PDF", t_corrupt_pdf),
        ("filename conflict", t_conflict),
        ("conflict resolve", t_resolve_conflict),
        ("ingest txt", t_ingest_txt),
        ("ingest markdown", t_ingest_md),
        ("ingest csv", t_ingest_csv),
        ("ingest json", t_ingest_json),
        ("ingest html", t_ingest_html),
        ("empty txt EMPTY_CONTENT", t_empty_txt_fails),
        ("unsupported mime", t_unsupported_mime),
        ("image without OCR", t_image_without_ocr),
        ("ocr-regions list", t_ocr_regions_list_empty_ok),
        ("search smoke", t_search_smoke),
    ]
    for name, fn in cases:
        report.results.append(run_case(name, fn))


def run_in_process() -> TestReport:
    import os
    import sys
    import tempfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.settings import get_settings
    from app.stores import db
    from app.stores.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine
    import asyncio

    tmp = tempfile.mkdtemp(prefix="rag-dynamic-")
    storage = Path(tmp) / "storage"
    storage.mkdir()
    os.environ["RAG_LOCAL_STORAGE_DIR"] = str(storage)
    os.environ["RAG_MODE"] = "ingest"
    os.environ["RAG_QDRANT_MOCK"] = "true"
    os.environ["RAG_MINIO_ENABLED"] = "false"
    os.environ["RAG_WORKER_EAGER"] = "true"
    get_settings.cache_clear()

    db_path = storage / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    db._engine = engine

    report = TestReport(
        mode="in-process",
        started_at=datetime.now(timezone.utc).isoformat(),
        base_url="asgi://in-process",
    )
    app = create_app()
    with TestClient(app) as client:
        execute_suite(client, report)
    return report


def run_live(base_url: str) -> TestReport:
    report = TestReport(
        mode="live",
        started_at=datetime.now(timezone.utc).isoformat(),
        base_url=base_url,
    )
    with httpx.Client(base_url=base_url, timeout=TIMEOUT) as client:
        execute_suite(client, report)
    return report


def write_reports(report: TestReport, out_dir: Path | None = None) -> tuple[Path, Path]:
    out_dir = out_dir or Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_test_report.json"
    md_path = out_dir / "dynamic_test_report.md"

    payload = {
        "mode": report.mode,
        "started_at": report.started_at,
        "base_url": report.base_url,
        "summary": {
            "passed": report.passed,
            "failed": report.failed,
            "total": len(report.results),
        },
        "results": [r.__dict__ for r in report.results],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# trustguard-rag 动态测试报告",
        "",
        f"- **时间**: {report.started_at}",
        f"- **模式**: {report.mode}",
        f"- **目标**: {report.base_url}",
        f"- **结果**: {report.passed}/{len(report.results)} 通过",
        "",
        "## 测试范围",
        "",
        "| 类别 | 用例 |",
        "|------|------|",
        "| 健康检查 | live / deps |",
        "| API 契约 | capabilities（多 MIME） |",
        "| PDF 主路径 | 入库 / ready / chunks / artifacts / 去重 / 损坏 / 冲突 |",
        "| 多格式 | txt / md / csv / json / html |",
        "| 错误路径 | 空文本 EMPTY_CONTENT、不支持 MIME、图片无 OCR |",
        "| OCR API | ocr-regions 列表 |",
        "| 检索 | search 冒烟（若端点存在） |",
        "",
        "## 明细",
        "",
        "| 状态 | 用例 | 耗时(ms) | 说明 |",
        "|------|------|----------|------|",
    ]
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        detail = (r.detail or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {mark} | {r.name} | {r.duration_ms} | {detail} |")
    lines.extend(["", "## 环境说明", ""])
    if report.mode == "in-process":
        lines.append(
            "- 使用 **in-process ASGI** + SQLite + 本地 blob（非 Docker live）。"
        )
        lines.append("- 缺口：未验证真实 MySQL / MinIO / RabbitMQ / OpenSearch 路径。")
    else:
        lines.append("- 针对运行中的 rag-service 做真实 HTTP 动态测试（Docker Compose live）。")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-process", action="store_true", help="Run against embedded ASGI app")
    parser.add_argument("--live-only", action="store_true", help="Fail if live service unavailable")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Report output directory (default: tests/)",
    )
    args = parser.parse_args()

    if args.in_process:
        report = run_in_process()
    else:
        try:
            httpx.get(f"{args.base_url}/health/live", timeout=3.0).raise_for_status()
            report = run_live(args.base_url)
        except Exception as e:
            if args.live_only:
                print(f"LIVE_UNAVAILABLE: {e}")
                return 2
            print("Live service unavailable, falling back to --in-process")
            report = run_in_process()

    out = Path(args.out_dir) if args.out_dir else Path(__file__).parent
    json_path, md_path = write_reports(report, out_dir=out)
    print(json.dumps({"passed": report.passed, "failed": report.failed, "json": str(json_path), "md": str(md_path)}, indent=2))
    for r in report.results:
        print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}: {r.detail}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
