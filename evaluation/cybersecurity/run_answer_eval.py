#!/usr/bin/env python3
"""调用 TrustGuard 回答 API，计算拒答、必要事实和引用指标。"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = Path(__file__).with_name("datasets") / "cybersecurity-dev.jsonl"
DEFAULT_RESULTS = Path(__file__).with_name("results")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:18200")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--name", default="answer-baseline")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--vector-top-k", type=int, default=30)
    parser.add_argument("--keyword-top-k", type=int, default=30)
    parser.add_argument("--fusion-method", choices=("rrf", "weighted_score"), default="rrf")
    parser.add_argument("--enable-vector", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-keyword", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-rerank", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def normalize_fact(value: str) -> str:
    """移除格式差异，令 2026-01-01 与 2026 年 1 月 1 日可稳定匹配。"""
    normalized = value.lower().translate(str.maketrans("", "", "年月日"))
    normalized = re.sub(r"\d+", lambda match: str(int(match.group())), normalized)
    return "".join(character for character in normalized if character.isalnum())


def citation_key(citation: dict[str, Any]) -> tuple[str, int | None]:
    return citation.get("original_filename") or "", citation.get("page_no")


def gold_keys(question: dict[str, Any]) -> set[tuple[str, int]]:
    return {(item["filename"], item["page"]) for item in question.get("relevant_evidence", [])}


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def evaluate_payload(question: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """对单条成功 API 响应计算无需外部裁判模型的确定性指标。"""
    status = payload.get("status")
    if status not in {"answered", "insufficient_evidence"}:
        raise ValueError(f"回答响应包含无效 status: {status!r}")
    answer = payload.get("answer")
    citations = payload.get("citations")
    if not isinstance(answer, str) or not isinstance(citations, list):
        raise ValueError("回答响应缺少 answer 或 citations")

    answerable = bool(question["answerable"])
    normalized_answer = normalize_fact(answer)
    must_include = question.get("must_include", [])
    matched_facts = [
        fact for fact in must_include if normalize_fact(str(fact)) in normalized_answer
    ]
    fact_recall = len(matched_facts) / len(must_include) if must_include else 1.0

    gold = gold_keys(question)
    cited = {citation_key(item) for item in citations}
    matched_citations = cited & gold
    citation_precision = len(matched_citations) / len(cited) if cited else 0.0
    citation_recall = len(matched_citations) / len(gold) if gold else 1.0
    status_correct = status == "answered" if answerable else status == "insufficient_evidence"

    return {
        "status": status,
        "status_correct": status_correct,
        "fact_recall": fact_recall if answerable else None,
        "all_required_facts": fact_recall == 1.0 if answerable else None,
        "citation_precision": citation_precision if answerable else None,
        "citation_recall": citation_recall if answerable else None,
        "matched_facts": matched_facts,
        "cited": [
            f"{filename}#page={page}"
            for filename, page in sorted(cited, key=lambda item: (item[0], item[1] or -1))
        ],
        "gold": [f"{filename}#page={page}" for filename, page in sorted(gold)],
        "answer": answer,
    }


def evaluate_question(
    client: httpx.Client,
    question: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.post("/v1/answer", json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("回答响应必须是 JSON 对象")
        metrics = evaluate_payload(question, payload)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        elapsed_ms = (time.perf_counter() - started) * 1000
        error_info: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
        if isinstance(error, httpx.HTTPStatusError):
            error_info["status_code"] = error.response.status_code
        return {
            "query_id": question["query_id"],
            "query": question["query"],
            "answerable": question["answerable"],
            "category": question["category"],
            "difficulty": question["difficulty"],
            "request_status": "failed",
            "wall_time_ms": elapsed_ms,
            "metrics": None,
            "error": error_info,
        }

    elapsed_ms = (time.perf_counter() - started) * 1000
    total_time_ms = payload.get("total_time_ms", elapsed_ms)
    if not isinstance(total_time_ms, (int, float)) or isinstance(total_time_ms, bool):
        total_time_ms = elapsed_ms
    return {
        "query_id": question["query_id"],
        "query": question["query"],
        "answerable": question["answerable"],
        "category": question["category"],
        "difficulty": question["difficulty"],
        "expected_answer": question.get("expected_answer"),
        "must_include": question.get("must_include", []),
        "request_status": "ok",
        "wall_time_ms": elapsed_ms,
        "total_time_ms": float(total_time_ms),
        "retrieval_time_ms": payload.get("retrieval_time_ms"),
        "generation_time_ms": payload.get("generation_time_ms"),
        "usage": payload.get("usage"),
        "model": payload.get("model"),
        "degraded_components": payload.get("degraded_components", []),
        "metrics": metrics,
    }


def summarize_queries(reports: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [item for item in reports if item["request_status"] == "ok"]
    answerable = [item for item in successful if item["answerable"]]
    unanswerable = [item for item in successful if not item["answerable"]]
    latencies = [item["total_time_ms"] for item in successful]
    token_totals = [
        item["usage"]["total_tokens"]
        for item in successful
        if isinstance(item.get("usage"), dict)
        and isinstance(item["usage"].get("total_tokens"), int)
    ]

    def mean(items: list[dict[str, Any]], key: str) -> float:
        return statistics.fmean(item["metrics"][key] for item in items) if items else 0.0

    return {
        "queries": len(reports),
        "successful_queries": len(successful),
        "failed_queries": len(reports) - len(successful),
        "answerable_queries": len(answerable),
        "unanswerable_queries": len(unanswerable),
        "status_accuracy": mean(successful, "status_correct"),
        "answer_rate_on_answerable": mean(answerable, "status_correct"),
        "refusal_accuracy": mean(unanswerable, "status_correct"),
        "required_fact_recall": mean(answerable, "fact_recall"),
        "all_required_facts_rate": mean(answerable, "all_required_facts"),
        "citation_precision": mean(answerable, "citation_precision"),
        "citation_recall": mean(answerable, "citation_recall"),
        "latency_mean_ms": statistics.fmean(latencies) if latencies else 0.0,
        "latency_p95_ms": percentile(latencies, 0.95),
        "mean_total_tokens": statistics.fmean(token_totals) if token_totals else None,
        "degraded_queries": sum(bool(item.get("degraded_components")) for item in successful),
    }


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"# 回答测评：{report['name']}",
        "",
        f"- 时间：`{report['created_at']}`",
        f"- 数据集：`{report['dataset']}`",
        f"- 成功/总请求：{summary['successful_queries']}/{summary['queries']}",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 回答/拒答状态准确率 | {summary['status_accuracy']:.4f} |",
        f"| 可回答问题回答率 | {summary['answer_rate_on_answerable']:.4f} |",
        f"| 不可回答问题拒答准确率 | {summary['refusal_accuracy']:.4f} |",
        f"| 必要事实召回率 | {summary['required_fact_recall']:.4f} |",
        f"| 完整覆盖必要事实比例 | {summary['all_required_facts_rate']:.4f} |",
        f"| 引用精确率 | {summary['citation_precision']:.4f} |",
        f"| 引用召回率 | {summary['citation_recall']:.4f} |",
        f"| 平均端到端延迟 | {summary['latency_mean_ms']:.1f} ms |",
        f"| P95 端到端延迟 | {summary['latency_p95_ms']:.1f} ms |",
        f"| 平均总 Token | {summary['mean_total_tokens'] or 0:.1f} |",
        f"| 失败请求 | {summary['failed_queries']} |",
        f"| 降级请求 | {summary['degraded_queries']} |",
        "",
        "## 异常与未通过问题",
        "",
    ]
    failed = [
        item
        for item in report["queries"]
        if item["request_status"] == "failed"
        or not item.get("metrics", {}).get("status_correct", False)
        or (item.get("answerable") and not item.get("metrics", {}).get("all_required_facts", False))
    ]
    if not failed:
        lines.append("无。")
    else:
        for item in failed:
            if item["request_status"] == "failed":
                detail = item.get("error", {}).get("message", "未知错误")
            else:
                metrics = item["metrics"]
                detail = (
                    f"status={metrics['status']}，fact_recall={metrics.get('fact_recall')}，"
                    f"citation_recall={metrics.get('citation_recall')}"
                )
            lines.append(f"- `{item['query_id']}` {item['query']}：{detail}")
    lines.extend(
        [
            "",
            "> 必要事实覆盖是确定性代理指标，不等同于语义正确性或事实忠实度。",
            "> 最终质量判断仍需人工抽检或另行引入经过校准的裁判模型。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    dataset = load_jsonl(args.dataset)
    config = {
        "top_k": args.top_k,
        "vector_top_k": args.vector_top_k,
        "keyword_top_k": args.keyword_top_k,
        "fusion_method": args.fusion_method,
        "enable_vector": args.enable_vector,
        "enable_keyword": args.enable_keyword,
        "enable_rerank": args.enable_rerank,
    }
    reports: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.api_url, timeout=args.timeout) as client:
        for question in dataset:
            reports.append(
                evaluate_question(
                    client,
                    question,
                    {"query": question["query"], **config},
                )
            )

    report = {
        "name": args.name,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset": str(args.dataset.resolve().relative_to(PROJECT_ROOT)),
        "config": config,
        "summary": summarize_queries(reports),
        "queries": reports,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / f"{args.name}.json"
    markdown_path = args.output_dir / f"{args.name}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_report(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"JSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    return 1 if report["summary"]["failed_queries"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
