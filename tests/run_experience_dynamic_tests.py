"""Experience Slice A live/in-process dynamic tests.

Usage:
  python tests/run_experience_dynamic_tests.py --live-only
  python tests/run_experience_dynamic_tests.py --in-process
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_BASE = "http://127.0.0.1:18200"
# Local embedding cold-start on full compose can exceed 2 minutes on first query.
TIMEOUT = 300.0

GATEWAY_TOKEN = os.environ.get("RAG_GATEWAY_SERVICE_TOKEN", "gateway-token")


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


def run_case(name: str, fn) -> CaseResult:
    t0 = time.perf_counter()
    try:
        detail = fn()
        passed = True
    except Exception as error:  # noqa: BLE001
        detail = str(error)
        passed = False
    return CaseResult(name, passed, detail, round((time.perf_counter() - t0) * 1000, 1))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upsert(external_id: str, revision: int = 1, **overrides) -> dict:
    body = {
        "schema_version": "trustguard-experience-upsert-v1",
        "external_id": external_id,
        "source_system": "trustguard-agent",
        "source_revision": revision,
        "knowledge_scope": "penetration",
        "workflow_type": "penetration",
        "experience_type": "skill_outcome",
        "workspace_id": None,
        "visibility": "global",
        "conditions": {"skill_id": "http-fingerprint", "marker": external_id},
        "action_summary": f"Reuse safe validation for {external_id}",
        "outcome_summary": f"Dynamic experience outcome {external_id}",
        "skill_id": "http-fingerprint",
        "phase": "recon",
        "source_task_id": f"task-{external_id}",
        "evidence_refs": [],
        "expires_at": None,
    }
    body.update(overrides)
    return body


def execute_suite(client: httpx.Client, report: TestReport) -> None:
    run_id = uuid.uuid4().hex[:10]
    external_id = f"dyn-exp-{run_id}"
    unique_phrase = f"ShiroRememberMe-{run_id}"
    state: dict = {}

    def t_health():
        r = client.get("/health/live")
        assert r.status_code == 200, r.text
        return "alive"

    def t_missing_token():
        r = client.put(f"/v1/experiences/{external_id}", json=_upsert(external_id))
        assert r.status_code == 401, r.text
        return "401"

    def t_upsert_candidate():
        r = client.put(
            f"/v1/experiences/{external_id}",
            headers=_auth(GATEWAY_TOKEN),
            json=_upsert(
                external_id,
                action_summary=f"Validate {unique_phrase} before exploit attempts",
                outcome_summary=f"Hit marker {unique_phrase} with low false positives",
            ),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "candidate"
        assert body["index_status"] == "not_indexed"
        state["id"] = body["id"]
        return body["id"]

    def t_candidate_not_in_search():
        r = client.post(
            "/v1/search/scope",
            headers=_auth(GATEWAY_TOKEN),
            json={
                "schema_version": "trustguard-knowledge-search-request-v1",
                "query": unique_phrase,
                "scope": "penetration",
                "limit": 10,
                "filters": {"content_types": [], "source_types": ["experience"]},
            },
        )
        assert r.status_code == 200, r.text
        hits = r.json().get("hits") or []
        assert all(state["id"] not in (h.get("document_id") or "") for h in hits)
        assert all(unique_phrase not in (h.get("snippet") or "") for h in hits)
        return f"hits={len(hits)}"

    def t_admin_promote():
        r = client.patch(
            f"/v1/experiences/{state['id']}/status",
            headers=_auth(GATEWAY_TOKEN),
            json={"status": "proven", "reason": "dynamic review"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "proven"
        assert body["index_status"] == "indexed", body
        return body["index_status"]

    def t_rollback_rejected():
        r = client.patch(
            f"/v1/experiences/{state['id']}/status",
            headers=_auth(GATEWAY_TOKEN),
            json={"status": "pending", "reason": "rollback attempt"},
        )
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "EXPERIENCE_CONFLICT"
        got = client.get(
            f"/v1/experiences/{state['id']}",
            headers=_auth(GATEWAY_TOKEN),
        )
        assert got.json()["status"] == "proven"
        return "rollback_rejected"

    def t_dual_index_present():
        if report.mode == "in-process":
            return "mock qdrant+opensearch (covered through scoped search)"
        experience_id = state["id"]
        # OpenSearch document must exist.
        os_host = os.environ.get("RAG_OPENSEARCH_HOST", "127.0.0.1")
        os_port = os.environ.get("RAG_OPENSEARCH_PORT", "18216")
        os_r = httpx.get(
            f"http://{os_host}:{os_port}/rag_chunks/_search",
            params={"q": f"document_id:{experience_id}", "size": "5"},
            timeout=30.0,
        )
        assert os_r.status_code == 200, os_r.text
        os_hits = (os_r.json().get("hits") or {}).get("hits") or []
        assert any(
            (h.get("_source") or {}).get("document_id") == experience_id for h in os_hits
        ), os_r.text

        # Qdrant scroll must find payload for this document_id.
        q_host = os.environ.get("RAG_QDRANT_HOST", "127.0.0.1")
        q_port = os.environ.get("RAG_QDRANT_PORT", "18214")
        collections = httpx.get(f"http://{q_host}:{q_port}/collections", timeout=30.0)
        assert collections.status_code == 200, collections.text
        names = [
            item["name"]
            for item in (collections.json().get("result") or {}).get("collections") or []
        ]
        found = False
        for name in names:
            if not str(name).startswith("rag_"):
                continue
            scroll = httpx.post(
                f"http://{q_host}:{q_port}/collections/{name}/points/scroll",
                json={
                    "limit": 20,
                    "with_payload": True,
                    "filter": {
                        "must": [
                            {
                                "key": "document_id",
                                "match": {"value": experience_id},
                            }
                        ]
                    },
                },
                timeout=30.0,
            )
            if scroll.status_code != 200:
                continue
            points = (scroll.json().get("result") or {}).get("points") or []
            if points:
                found = True
                break
        assert found, f"experience {experience_id} missing in qdrant collections={names}"
        return "qdrant+opensearch"

    def t_proven_searchable():
        # Allow brief index visibility lag on live backends.
        deadline = time.time() + 20
        last = None
        while time.time() < deadline:
            r = client.post(
                "/v1/search/scope",
                headers=_auth(GATEWAY_TOKEN),
                json={
                    "schema_version": "trustguard-knowledge-search-request-v1",
                    "query": unique_phrase,
                    "scope": "penetration",
                    "limit": 10,
                    "filters": {"content_types": [], "source_types": ["experience"]},
                },
            )
            assert r.status_code == 200, r.text
            last = r.json()
            hits = last.get("hits") or []
            matched = [
                h
                for h in hits
                if h.get("source_type") == "experience"
                and (
                    state["id"] == h.get("document_id")
                    or unique_phrase in (h.get("snippet") or "")
                )
            ]
            if matched:
                return f"matched={len(matched)}"
            time.sleep(0.5)
        raise AssertionError(f"proven experience not found in search: {last}")

    def t_higher_revision_updates():
        updated_phrase = f"{unique_phrase}-rev2"
        r = client.put(
            f"/v1/experiences/{external_id}",
            headers=_auth(GATEWAY_TOKEN),
            json=_upsert(
                external_id,
                revision=2,
                action_summary=f"Updated action {updated_phrase}",
                outcome_summary=f"Updated outcome {updated_phrase}",
            ),
        )
        assert r.status_code == 200, r.text
        assert r.json()["source_revision"] == 2
        assert r.json()["status"] == "proven"
        state["updated_phrase"] = updated_phrase
        deadline = time.time() + 20
        while time.time() < deadline:
            s = client.post(
                "/v1/search/scope",
                headers=_auth(GATEWAY_TOKEN),
                json={
                    "schema_version": "trustguard-knowledge-search-request-v1",
                    "query": updated_phrase,
                    "scope": "penetration",
                    "limit": 10,
                    "filters": {"content_types": [], "source_types": ["experience"]},
                },
            )
            assert s.status_code == 200, s.text
            hits = s.json().get("hits") or []
            if any(updated_phrase in (h.get("snippet") or "") for h in hits):
                return "updated"
            time.sleep(0.5)
        raise AssertionError("updated proven content not searchable")

    def t_stale_revision():
        r = client.put(
            f"/v1/experiences/{external_id}",
            headers=_auth(GATEWAY_TOKEN),
            json=_upsert(external_id, revision=1),
        )
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "EXPERIENCE_STALE_REVISION"
        return "stale_rejected"

    def t_alert_triage_disabled():
        eid = f"{external_id}-at"
        r = client.put(
            f"/v1/experiences/{eid}",
            headers=_auth(GATEWAY_TOKEN),
            json=_upsert(
                eid,
                knowledge_scope="alert-triage",
                workflow_type="alert-triage",
            ),
        )
        assert r.status_code == 422, r.text
        assert r.json()["code"] == "EXPERIENCE_SCOPE_NOT_ENABLED"
        return "not_enabled"

    def t_feedback():
        event_id = f"evt-{run_id}"
        body = {
            "schema_version": "trustguard-experience-feedback-v1",
            "event_id": event_id,
            "experience_id": state["id"],
            "task_id": f"task-{run_id}",
            "workflow_type": "penetration",
            "outcome": "success",
            "evidence_level": "verified",
            "notes": "dynamic",
            "occurred_at": None,
        }
        first = client.post(
            f"/v1/experiences/{state['id']}/feedback",
            headers=_auth(GATEWAY_TOKEN),
            json=body,
        )
        assert first.status_code == 200, first.text
        assert first.json()["duplicated"] is False
        assert first.json()["experience_status"] == "proven"
        second = client.post(
            f"/v1/experiences/{state['id']}/feedback",
            headers=_auth(GATEWAY_TOKEN),
            json=body,
        )
        assert second.status_code == 200, second.text
        assert second.json()["duplicated"] is True
        got = client.get(
            f"/v1/experiences/{state['id']}",
            headers=_auth(GATEWAY_TOKEN),
        )
        assert got.json()["status"] == "proven"
        assert got.json()["success_count"] == 1
        return "feedback_ok"

    def t_deprecate_unsearchable():
        r = client.patch(
            f"/v1/experiences/{state['id']}/status",
            headers=_auth(GATEWAY_TOKEN),
            json={"status": "deprecated", "reason": "outdated"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "deprecated"
        assert r.json()["index_status"] == "not_indexed"
        phrase = state.get("updated_phrase", unique_phrase)
        deadline = time.time() + 20
        while time.time() < deadline:
            s = client.post(
                "/v1/search/scope",
                headers=_auth(GATEWAY_TOKEN),
                json={
                    "schema_version": "trustguard-knowledge-search-request-v1",
                    "query": phrase,
                    "scope": "penetration",
                    "limit": 10,
                    "filters": {"content_types": [], "source_types": ["experience"]},
                },
            )
            assert s.status_code == 200, s.text
            hits = s.json().get("hits") or []
            still = [
                h
                for h in hits
                if state["id"] == h.get("document_id")
                or phrase in (h.get("snippet") or "")
            ]
            if not still:
                break
            time.sleep(0.5)
        else:
            raise AssertionError("deprecated experience still searchable")

        if report.mode == "in-process":
            return "removed from mock search indexes"

        experience_id = state["id"]
        os_host = os.environ.get("RAG_OPENSEARCH_HOST", "127.0.0.1")
        os_port = os.environ.get("RAG_OPENSEARCH_PORT", "18216")
        os_r = httpx.get(
            f"http://{os_host}:{os_port}/rag_chunks/_search",
            params={"q": f"document_id:{experience_id}", "size": "5"},
            timeout=30.0,
        )
        assert os_r.status_code == 200, os_r.text
        os_hits = (os_r.json().get("hits") or {}).get("hits") or []
        assert not any(
            (h.get("_source") or {}).get("document_id") == experience_id for h in os_hits
        ), "opensearch still has deprecated experience"

        q_host = os.environ.get("RAG_QDRANT_HOST", "127.0.0.1")
        q_port = os.environ.get("RAG_QDRANT_PORT", "18214")
        collections = httpx.get(f"http://{q_host}:{q_port}/collections", timeout=30.0)
        names = [
            item["name"]
            for item in (collections.json().get("result") or {}).get("collections") or []
        ]
        for name in names:
            if not str(name).startswith("rag_"):
                continue
            scroll = httpx.post(
                f"http://{q_host}:{q_port}/collections/{name}/points/scroll",
                json={
                    "limit": 5,
                    "with_payload": True,
                    "filter": {
                        "must": [
                            {
                                "key": "document_id",
                                "match": {"value": experience_id},
                            }
                        ]
                    },
                },
                timeout=30.0,
            )
            if scroll.status_code != 200:
                continue
            points = (scroll.json().get("result") or {}).get("points") or []
            assert not points, f"qdrant still has deprecated experience in {name}"
        return "removed_both_indexes"

    cases = [
        ("health_live", t_health),
        ("auth_missing_token", t_missing_token),
        ("upsert_candidate", t_upsert_candidate),
        ("candidate_not_in_search", t_candidate_not_in_search),
        ("admin_promote_indexed", t_admin_promote),
        ("rollback_http_rejected", t_rollback_rejected),
        ("dual_index_present", t_dual_index_present),
        ("proven_searchable", t_proven_searchable),
        ("higher_revision_updates_search", t_higher_revision_updates),
        ("stale_revision_rejected", t_stale_revision),
        ("alert_triage_not_enabled", t_alert_triage_disabled),
        ("feedback_idempotent", t_feedback),
        ("deprecate_unsearchable", t_deprecate_unsearchable),
    ]
    for name, fn in cases:
        report.results.append(run_case(name, fn))


def run_live(base_url: str) -> TestReport:
    report = TestReport(
        mode="live",
        started_at=datetime.now(timezone.utc).isoformat(),
        base_url=base_url,
    )
    with httpx.Client(base_url=base_url, timeout=TIMEOUT) as client:
        execute_suite(client, report)
    return report


def run_in_process() -> TestReport:
    os.environ["RAG_EXPERIENCE_ENABLED"] = "true"
    os.environ["RAG_QDRANT_MOCK"] = "true"
    os.environ["RAG_SEARCH_OPENSEARCH_MOCK"] = "true"
    os.environ["RAG_EMBEDDING_PROVIDER"] = "pseudo"
    os.environ["RAG_RERANK_PROVIDER"] = "none"
    os.environ["RAG_QUERY_PLANNER_LLM_ENABLED"] = "false"
    os.environ["RAG_GATEWAY_AUTH_ENABLED"] = "true"
    os.environ["RAG_GATEWAY_SERVICE_TOKEN"] = GATEWAY_TOKEN
    os.environ["RAG_PDF_PARSER"] = "local"
    os.environ["RAG_MINIO_ENABLED"] = "false"
    os.environ["RAG_WORKER_EAGER"] = "true"

    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.main import create_app
    from app.settings import get_settings
    from app.stores import db
    from app.stores.models import Base

    get_settings.cache_clear()
    report = TestReport(
        mode="in-process",
        started_at=datetime.now(timezone.utc).isoformat(),
        base_url="http://test",
    )
    db_path = Path(__file__).resolve().parent / "_experience_dyn.sqlite"
    if db_path.exists():
        db_path.unlink()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async def _prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.run(_prepare())
    db._engine = engine  # type: ignore[attr-defined]
    app = create_app()
    with TestClient(app, base_url="http://test") as client:
        execute_suite(client, report)
    asyncio.run(engine.dispose())
    db._engine = None  # type: ignore[attr-defined]
    get_settings.cache_clear()
    return report


def write_reports(report: TestReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = out_dir / f"experience-slice-a-dynamic-{stamp}.json"
    md_path = out_dir / f"experience-slice-a-dynamic-{stamp}.md"
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
        "# Experience Slice A 动态测试报告",
        "",
        f"- **时间**: {report.started_at}",
        f"- **模式**: {report.mode}",
        f"- **目标**: {report.base_url}",
        f"- **结果**: {report.passed}/{len(report.results)} 通过",
        "",
        "| 状态 | 用例 | 耗时(ms) | 说明 |",
        "|------|------|----------|------|",
    ]
    for item in report.results:
        mark = "PASS" if item.passed else "FAIL"
        detail = (item.detail or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {mark} | {item.name} | {item.duration_ms} | {detail} |")
    lines.extend(
        [
            "",
            "## 覆盖说明",
            "",
            "- Upsert candidate、auth 负向、admin 晋升、proven 检索、revision、alert-triage 未启用、feedback 幂等、deprecate 摘索引",
            "",
            "## 环境说明",
            "",
        ]
    )
    if report.mode == "in-process":
        lines.append("- in-process ASGI + SQLite + mock Qdrant/OpenSearch（非 Docker live）。")
        lines.append("- 缺口：未验证真实 MySQL / Qdrant / OpenSearch 双写路径。")
    else:
        lines.append("- Docker Compose live HTTP 动态测试。")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in-process", action="store_true")
    parser.add_argument("--live-only", action="store_true")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument(
        "--out-dir",
        default=str(
            Path(__file__).resolve().parents[2]
            / "local-notes"
            / "trustguard-rag"
        ),
    )
    args = parser.parse_args()

    if args.in_process:
        report = run_in_process()
    else:
        try:
            httpx.get(f"{args.base_url}/health/live", timeout=3.0).raise_for_status()
            report = run_live(args.base_url)
        except Exception as error:
            if args.live_only:
                print(f"LIVE_UNAVAILABLE: {error}")
                return 2
            print(f"Live unavailable ({error}); falling back to --in-process")
            report = run_in_process()

    json_path, md_path = write_reports(report, Path(args.out_dir))
    print(
        json.dumps(
            {
                "passed": report.passed,
                "failed": report.failed,
                "mode": report.mode,
                "json": str(json_path),
                "md": str(md_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    for item in report.results:
        print(f"[{'PASS' if item.passed else 'FAIL'}] {item.name}: {item.detail}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
