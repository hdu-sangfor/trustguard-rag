from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "evaluation" / "cybersecurity" / "run_answer_eval.py"
)
SPEC = importlib.util.spec_from_file_location("run_answer_eval", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
answer_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(answer_eval)


def _question(answerable: bool = True) -> dict:
    return {
        "query_id": "Q-1",
        "query": "施行日期是什么？",
        "category": "policy",
        "difficulty": "easy",
        "answerable": answerable,
        "must_include": ["2026-01-01"] if answerable else [],
        "relevant_evidence": ([{"filename": "policy.pdf", "page": 2}] if answerable else []),
    }


def test_evaluate_payload_matches_formatted_fact_and_citation() -> None:
    result = answer_eval.evaluate_payload(
        _question(),
        {
            "status": "answered",
            "answer": "该规定自 2026 年 1 月 1 日施行。[1]",
            "citations": [{"original_filename": "policy.pdf", "page_no": 2}],
        },
    )

    assert result["status_correct"] is True
    assert result["fact_recall"] == 1.0
    assert result["citation_precision"] == 1.0
    assert result["citation_recall"] == 1.0


def test_citation_precision_accepts_cross_dataset_gold_without_inflating_recall() -> None:
    question = _question()
    question["acceptable_evidence"] = [
        {"filename": "policy.pdf", "page": 2},
        {"filename": "hard-compare.pdf", "page": 7},
    ]

    result = answer_eval.evaluate_payload(
        question,
        {
            "status": "answered",
            "answer": "该规定自 2026 年 1 月 1 日施行。[1]",
            "citations": [{"original_filename": "hard-compare.pdf", "page_no": 7}],
        },
    )

    assert result["citation_precision"] == 1.0
    assert result["citation_recall"] == 0.0
    assert result["gold"] == ["policy.pdf#page=2"]
    assert result["acceptable_gold"] == [
        "hard-compare.pdf#page=7",
        "policy.pdf#page=2",
    ]


def test_evaluate_payload_scores_correct_refusal() -> None:
    result = answer_eval.evaluate_payload(
        _question(answerable=False),
        {
            "status": "insufficient_evidence",
            "answer": "证据不足。",
            "citations": [],
        },
    )

    assert result["status_correct"] is True
    assert result["fact_recall"] is None


def test_summarize_queries_keeps_answer_and_refusal_metrics_separate() -> None:
    reports = [
        {
            "request_status": "ok",
            "answerable": True,
            "total_time_ms": 100.0,
            "usage": {"total_tokens": 50},
            "degraded_components": [],
            "metrics": {
                "status_correct": True,
                "fact_recall": 1.0,
                "all_required_facts": True,
                "citation_precision": 1.0,
                "citation_recall": 0.5,
            },
        },
        {
            "request_status": "ok",
            "answerable": False,
            "total_time_ms": 80.0,
            "usage": {"total_tokens": 20},
            "degraded_components": [],
            "metrics": {
                "status_correct": True,
                "fact_recall": None,
                "all_required_facts": None,
                "citation_precision": None,
                "citation_recall": None,
            },
        },
    ]

    summary = answer_eval.summarize_queries(reports)

    assert summary["status_accuracy"] == 1.0
    assert summary["refusal_accuracy"] == 1.0
    assert summary["citation_recall"] == 0.5
    assert summary["mean_total_tokens"] == 35


def test_evaluate_question_records_safe_api_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            request=request,
            json={"detail": "Answered response must contain at least one citation"},
        )

    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        report = answer_eval.evaluate_question(
            client,
            _question(),
            {"query": "施行日期是什么？"},
        )

    assert report["request_status"] == "failed"
    assert report["error"]["status_code"] == 502
    assert report["error"]["response_detail"] == (
        "Answered response must contain at least one citation"
    )
