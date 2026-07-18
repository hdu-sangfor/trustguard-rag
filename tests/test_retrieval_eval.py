"""检索评测脚本的失败记录与汇总测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import httpx


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "evaluation"
    / "cybersecurity"
    / "run_retrieval_eval.py"
)
SPEC = importlib.util.spec_from_file_location("run_retrieval_eval", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
retrieval_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retrieval_eval)


def question(query_id: str, query: str) -> dict:
    return {
        "query_id": query_id,
        "query": query,
        "category": "security",
        "difficulty": "hard",
        "answerable": True,
        "relevant_evidence": [{"filename": "evidence.pdf", "page": 1}],
    }


def test_evaluate_question_records_http_failure_as_zero_score() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "search unavailable"}, request=request)

    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        report = retrieval_eval.evaluate_question(
            client,
            question("Q-1", "失败查询"),
            {"query": "失败查询"},
        )

    assert report["search_status"] == "failed"
    assert report["error"]["type"] == "HTTPStatusError"
    assert report["error"]["status_code"] == 503
    assert report["metrics"]["hit@1"] == 0.0
    assert report["metrics"]["recall@10"] == 0.0
    assert report["results"] == []


def test_evaluate_question_records_timeout_and_invalid_json() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request timed out", request=request)

    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(timeout_handler),
    ) as client:
        timeout_report = retrieval_eval.evaluate_question(
            client,
            question("Q-1", "超时查询"),
            {"query": "超时查询"},
        )

    def invalid_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(invalid_json_handler),
    ) as client:
        invalid_json_report = retrieval_eval.evaluate_question(
            client,
            question("Q-2", "无效响应"),
            {"query": "无效响应"},
        )

    assert timeout_report["search_status"] == "failed"
    assert timeout_report["error"]["type"] == "ReadTimeout"
    assert invalid_json_report["search_status"] == "failed"
    assert invalid_json_report["error"]["type"] == "JSONDecodeError"


def test_main_continues_after_failed_query_and_reports_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in [question("Q-1", "失败查询"), question("Q-2", "成功查询")]
        )
        + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "results"
    args = argparse.Namespace(
        api_url="http://test",
        dataset=dataset,
        output_dir=output_dir,
        name="failure-test",
        top_k=10,
        vector_top_k=30,
        keyword_top_k=30,
        fusion_method="rrf",
        enable_vector=True,
        enable_keyword=True,
        enable_rerank=False,
        timeout=1.0,
    )
    monkeypatch.setattr(retrieval_eval, "parse_args", lambda: args)
    monkeypatch.setattr(retrieval_eval, "PROJECT_ROOT", tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        payload = json.loads(request.content)
        if payload["query"] == "失败查询":
            return httpx.Response(503, json={"detail": "search unavailable"}, request=request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "source": {
                            "original_filename": "evidence.pdf",
                            "page_no": 1,
                        }
                    }
                ],
                "retrieval_time_ms": 12.5,
                "search_status": "ok",
                "effective_mode": "hybrid",
                "components": {},
                "degraded_components": [],
            },
            request=request,
        )

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return real_client(**kwargs, transport=transport)

    monkeypatch.setattr(retrieval_eval.httpx, "Client", client_factory)

    assert retrieval_eval.main() == 0

    report = json.loads((output_dir / "failure-test.json").read_text(encoding="utf-8"))
    assert report["summary"]["queries"] == 2
    assert report["summary"]["successful_queries"] == 1
    assert report["summary"]["failed_queries"] == 1
    assert report["summary"]["failure_rate"] == 0.5
    assert report["summary"]["hit@1"] == 0.5
    assert [item["search_status"] for item in report["queries"]] == ["failed", "ok"]

    markdown = (output_dir / "failure-test.md").read_text(encoding="utf-8")
    assert "| 失败请求 | 1 |" in markdown
    assert "`Q-1` 失败查询" in markdown
