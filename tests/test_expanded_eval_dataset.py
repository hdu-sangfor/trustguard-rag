"""扩展网络安全检索评测集的结构与标注完整性测试。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.skip(reason="replaced by crawler V2 dataset quality tests")

DATASET_ROOT = Path(__file__).resolve().parents[1] / "evaluation" / "cybersecurity"


def load_source(name: str) -> dict:
    return json.loads((DATASET_ROOT / name).read_text(encoding="utf-8"))


def test_expanded_source_has_balanced_scale_and_difficulty() -> None:
    data = load_source("source_data.json")
    documents = [
        document for document in data["documents"] if document["document_id"].startswith("DOC-")
    ][-4:]
    sections = [section for document in documents for section in document["sections"]]
    questions = [question for question in data["questions"] if question["query_id"].startswith("EXP-")]

    assert len(documents) == 4
    assert len(sections) == 24
    assert len(questions) == 48
    assert Counter(question["split"] for question in questions) == {"dev": 24, "test": 24}
    assert sum(not question["answerable"] for question in questions) == 4
    assert Counter(
        question["split"] for question in questions if not question["answerable"]
    ) == {"dev": 2, "test": 2}

    difficulties = {question["difficulty"] for question in questions}
    assert {
        "basic",
        "semantic",
        "bilingual_semantic",
        "multi_hop",
        "reasoning",
        "negation",
        "hard_negative",
        "identifier",
        "unanswerable",
    } <= difficulties
    for split in ("dev", "test"):
        split_difficulties = {
            question["difficulty"] for question in questions if question["split"] == split
        }
        assert "basic" in split_difficulties
        assert split_difficulties - {"basic"}


def test_all_sources_have_unique_ids_and_complete_gold_references() -> None:
    source = load_source("source_data.json")
    documents = source["documents"]
    sections = [section for document in documents for section in document["sections"]]
    questions = source["questions"]
    evidence_ids = [section["evidence_id"] for section in sections]
    query_ids = [question["query_id"] for question in questions]
    evidence_set = set(evidence_ids)

    assert len(documents) == 9
    assert len(sections) == 53
    assert len(questions) == 108
    assert len(evidence_ids) == len(evidence_set)
    assert len(query_ids) == len(set(query_ids))

    for question in questions:
        assert set(question["evidence_ids"]) <= evidence_set
        if question["answerable"]:
            assert question["evidence_ids"]
            assert question["expected_answer"].strip()
            assert question["must_include"]
        else:
            assert question["evidence_ids"] == []


def test_expanded_references_use_named_primary_sources() -> None:
    data = load_source("source_data.json")
    sections = [
        section
        for document in data["documents"][-4:]
        for section in document["sections"]
    ]

    for section in sections:
        assert section["sources"]
        for label, url in section["sources"]:
            assert label.strip()
            assert url.startswith("https://")
