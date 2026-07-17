#!/usr/bin/env python3
"""调用 TrustGuard 搜索 API 并计算网络安全开发集的检索指标。"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = Path(__file__).with_name("datasets") / "cybersecurity-dev.jsonl"
DEFAULT_RESULTS = Path(__file__).with_name("results")
KS = (1, 3, 5, 10)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:18200")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--name", default="hybrid-rrf-rerank")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--vector-top-k", type=int, default=30)
    parser.add_argument("--keyword-top-k", type=int, default=30)
    parser.add_argument("--fusion-method", choices=("rrf", "weighted_score"), default="rrf")
    parser.add_argument("--enable-vector", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-keyword", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-rerank", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--timeout", type=float, default=90.0)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def result_key(result: dict[str, Any]) -> tuple[str, int | None]:
    source = result["source"]
    return source.get("original_filename") or "", source.get("page_no")


def gold_keys(question: dict[str, Any]) -> set[tuple[str, int]]:
    return {
        (evidence["filename"], evidence["page"])
        for evidence in question.get("relevant_evidence", [])
    }


def query_metrics(results: list[dict[str, Any]], gold: set[tuple[str, int]]) -> dict[str, float]:
    ranked = [result_key(result) for result in results]
    first_rank = next((rank for rank, key in enumerate(ranked, start=1) if key in gold), None)
    metrics: dict[str, float] = {"reciprocal_rank": 0.0 if first_rank is None else 1.0 / first_rank}
    for k in KS:
        top = ranked[:k]
        matched = set(top) & gold
        dcg = sum(1.0 / math.log2(rank + 1) for rank, key in enumerate(top, start=1) if key in gold)
        ideal_count = min(len(gold), k)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        metrics[f"hit@{k}"] = float(bool(matched))
        metrics[f"recall@{k}"] = len(matched) / len(gold)
        metrics[f"ndcg@{k}"] = dcg / idcg if idcg else 0.0
    return metrics


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    config = report["config"]
    lines = [
        f"# 检索测评：{report['name']}",
        "",
        f"- 时间：`{report['created_at']}`",
        f"- 数据集：`{report['dataset']}`",
        f"- 可回答问题：{summary['answerable_queries']}",
        f"- 不可回答问题：{summary['unanswerable_queries']}（当前只记录，不纳入检索相关性指标）",
        f"- 配置：向量={config['enable_vector']}，关键词={config['enable_keyword']}，"
        f"Rerank={config['enable_rerank']}，融合={config['fusion_method']}",
        "",
        "## 汇总指标",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
    ]
    for key in ("hit@1", "hit@3", "hit@5", "hit@10", "recall@10", "mrr", "ndcg@10"):
        lines.append(f"| {key} | {summary[key]:.4f} |")
    lines.extend(
        [
            f"| 平均延迟 | {summary['latency_mean_ms']:.1f} ms |",
            f"| P95 延迟 | {summary['latency_p95_ms']:.1f} ms |",
            f"| 降级请求 | {summary['degraded_queries']} |",
            "",
            "## 未命中问题（Top 10）",
            "",
        ]
    )
    misses = [item for item in report["queries"] if item.get("answerable") and item["metrics"]["hit@10"] == 0]
    if not misses:
        lines.append("无。")
    else:
        for item in misses:
            lines.append(f"- `{item['query_id']}` {item['query']}")
            lines.append(f"  - 标准证据：{', '.join(item['gold'])}")
            lines.append(f"  - Top 3：{', '.join(item['retrieved'][:3])}")
    lines.extend(["", "## Top 1 未命中问题", ""])
    top1_misses = [
        item for item in report["queries"] if item.get("answerable") and item["metrics"]["hit@1"] == 0
    ]
    if not top1_misses:
        lines.append("无。")
    else:
        for item in top1_misses:
            lines.append(f"- `{item['query_id']}` {item['query']} → {item['retrieved'][0] if item['retrieved'] else '空结果'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    if not args.enable_vector and not args.enable_keyword:
        raise SystemExit("向量和关键词检索不能同时关闭")

    questions = load_jsonl(args.dataset.resolve())
    query_reports: list[dict[str, Any]] = []
    with httpx.Client(base_url=args.api_url.rstrip("/"), timeout=args.timeout) as client:
        health = client.get("/health")
        health.raise_for_status()
        for index, question in enumerate(questions, start=1):
            body = {
                "query": question["query"],
                "top_k": args.top_k,
                "vector_top_k": args.vector_top_k,
                "keyword_top_k": args.keyword_top_k,
                "fusion_method": args.fusion_method,
                "enable_vector": args.enable_vector,
                "enable_keyword": args.enable_keyword,
                "enable_rerank": args.enable_rerank,
            }
            started = time.perf_counter()
            response = client.post("/v1/search", json=body)
            elapsed_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            payload = response.json()
            gold = gold_keys(question)
            metrics = query_metrics(payload["results"], gold) if question["answerable"] else None
            retrieved = [
                f"{filename}#page={page}" for filename, page in map(result_key, payload["results"])
            ]
            query_reports.append(
                {
                    "query_id": question["query_id"],
                    "query": question["query"],
                    "category": question["category"],
                    "difficulty": question["difficulty"],
                    "answerable": question["answerable"],
                    "gold": [f"{filename}#page={page}" for filename, page in sorted(gold)],
                    "retrieved": retrieved,
                    "metrics": metrics,
                    "latency_ms": payload.get("retrieval_time_ms", elapsed_ms),
                    "wall_time_ms": elapsed_ms,
                    "search_status": payload.get("search_status"),
                    "effective_mode": payload.get("effective_mode"),
                    "components": payload.get("components", {}),
                    "degraded_components": payload.get("degraded_components", []),
                    "results": payload["results"],
                }
            )
            print(
                f"[{index:02d}/{len(questions)}] {question['query_id']} "
                f"status={payload.get('search_status')} results={len(payload['results'])} "
                f"wall={elapsed_ms:.0f}ms"
            )

    scored = [item for item in query_reports if item["answerable"]]
    latencies = [float(item["latency_ms"]) for item in query_reports]
    summary = {
        "queries": len(query_reports),
        "answerable_queries": len(scored),
        "unanswerable_queries": len(query_reports) - len(scored),
        "hit@1": statistics.fmean(item["metrics"]["hit@1"] for item in scored),
        "hit@3": statistics.fmean(item["metrics"]["hit@3"] for item in scored),
        "hit@5": statistics.fmean(item["metrics"]["hit@5"] for item in scored),
        "hit@10": statistics.fmean(item["metrics"]["hit@10"] for item in scored),
        "recall@10": statistics.fmean(item["metrics"]["recall@10"] for item in scored),
        "mrr": statistics.fmean(item["metrics"]["reciprocal_rank"] for item in scored),
        "ndcg@10": statistics.fmean(item["metrics"]["ndcg@10"] for item in scored),
        "latency_mean_ms": statistics.fmean(latencies),
        "latency_p95_ms": percentile(latencies, 0.95),
        "degraded_queries": sum(bool(item["degraded_components"]) for item in query_reports),
        "failed_queries": sum(item["search_status"] == "failed" for item in query_reports),
    }
    report = {
        "name": args.name,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset": str(args.dataset.resolve().relative_to(PROJECT_ROOT)),
        "api_url": args.api_url,
        "config": {
            "top_k": args.top_k,
            "vector_top_k": args.vector_top_k,
            "keyword_top_k": args.keyword_top_k,
            "fusion_method": args.fusion_method,
            "enable_vector": args.enable_vector,
            "enable_keyword": args.enable_keyword,
            "enable_rerank": args.enable_rerank,
        },
        "summary": summary,
        "queries": query_reports,
    }

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{args.name}.json"
    markdown_path = output_dir / f"{args.name}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_report(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
