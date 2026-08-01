"""Crawler V2 评测集的结构、标注和数据泄漏测试。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


DATASET_ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "crawler"
SECURITY_ID = re.compile(
    r"(?<![A-Z0-9])(?:CVE-\d{4}-\d{4,}|CWE-\d+|CAPEC-\d+)(?!\d)",
    re.IGNORECASE,
)


def load_json(name: str):
    return json.loads((DATASET_ROOT / "datasets" / name).read_text(encoding="utf-8"))


def load_questions() -> list[dict]:
    return [
        json.loads(line)
        for line in (
            DATASET_ROOT / "datasets" / "crawler-eval.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_v2_dataset_has_balanced_retrieval_slices() -> None:
    stats = load_json("stats.json")
    questions = load_questions()

    assert stats["dataset_version"] == 2
    assert stats["documents"] >= 100
    assert len(questions) >= 125
    assert 0.15 <= stats["unanswerable_questions"] / len(questions) <= 0.25
    assert stats["explicit_identifier_questions"] / len(questions) <= 0.40
    assert {
        "semantic",
        "exact_lookup",
        "confusion",
        "unanswerable",
    } <= set(stats["query_types"])
    assert {
        "semantic_without_identifier",
        "exact_identifier",
        "identifier_confusion",
        "unanswerable_identifier",
        "unanswerable_semantic",
    } <= set(stats["evaluation_slices"])


def test_queries_are_unique_and_gold_is_consistent() -> None:
    questions = load_questions()
    normalized_queries = [
        re.sub(r"\s+", " ", item["query"]).strip().casefold()
        for item in questions
    ]

    assert len(normalized_queries) == len(set(normalized_queries))
    for question in questions:
        if question["answerable"]:
            assert question["relevant_evidence"]
            assert question["relevance_groups"]
            assert question["evidence_ids"]
        else:
            assert question["relevant_evidence"] == []
            assert question["relevance_groups"] == []
            assert question["evidence_ids"] == []


def test_semantic_slice_does_not_leak_security_identifiers() -> None:
    questions = load_questions()
    semantic = [
        item
        for item in questions
        if item["evaluation_slice"] == "semantic_without_identifier"
    ]

    assert semantic
    assert all(not SECURITY_ID.search(item["query"]) for item in semantic)
    assert all(not item["contains_explicit_identifier"] for item in semantic)


def test_manifest_is_deduplicated_and_traceable_to_crawler_docs() -> None:
    manifest = load_json("corpus-manifest.json")
    excluded = load_json("excluded-sources.json")

    security_ids = [
        item["security_id"] for item in manifest if item["security_id"] is not None
    ]
    assert len(security_ids) == len(set(security_ids))
    assert Counter(item["reason"] for item in excluded)["duplicate_security_id"] >= 1
    assert all(item["source_path"] for item in manifest)
    assert all(item["source_sha256"] for item in manifest)
