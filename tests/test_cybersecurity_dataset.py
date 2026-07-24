from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_DIR = PROJECT_ROOT / "evaluation" / "cybersecurity"
SCRIPT_PATH = EVALUATION_DIR / "build_dataset.py"
SPEC = importlib.util.spec_from_file_location("build_cybersecurity_dataset", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
build_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_module)


def _source() -> dict:
    return json.loads((EVALUATION_DIR / "source.json").read_text(encoding="utf-8"))


def test_unified_source_is_valid_and_has_expected_size() -> None:
    source = _source()

    build_module.validate_source(source)

    evidence_ids = {
        section["evidence_id"]
        for document in source["documents"]
        for section in document["sections"]
    }
    evidence_ids.update(item["evidence_id"] for item in source["external_evidence"])
    assert source["dataset"]["version"] == "0.3.0"
    assert len(source["documents"]) == 5
    assert len(evidence_ids) == 29
    assert len(source["questions"]) == 60
    assert sum(item["split"] == "dev" for item in source["questions"]) == 30
    assert sum(item["split"] == "test" for item in source["questions"]) == 30
    assert sum(item["answerable"] for item in source["questions"]) == 55
    assert sum(
        set(item["acceptable_evidence_ids"]) != set(item["evidence_ids"])
        for item in source["questions"]
    ) == 33


def test_public_test_row_hides_all_answer_and_gold_fields() -> None:
    row = {
        "query_id": "Q-1",
        "query": "问题",
        "answerable": True,
        "evidence_ids": ["E-1"],
        "expected_answer": "答案",
        "must_include": ["答案"],
        "relevant_evidence": [{"evidence_id": "E-1"}],
        "acceptable_evidence_ids": ["E-1", "E-2"],
        "acceptable_evidence": [{"evidence_id": "E-1"}],
    }

    public = build_module.public_test_row(row)

    assert public == {
        "query_id": "Q-1",
        "query": "问题",
    }


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda question: question.update(acceptable_evidence_ids=["UNKNOWN"]),
            "不存在的证据",
        ),
        (
            lambda question: question.update(acceptable_evidence_ids=[]),
            "必须包含全部必需证据",
        ),
    ],
)
def test_invalid_acceptable_evidence_is_rejected(mutate, message: str) -> None:
    source = copy.deepcopy(_source())
    question = next(item for item in source["questions"] if item["answerable"])
    mutate(question)

    with pytest.raises(ValueError, match=message):
        build_module.validate_source(source)


def test_unanswerable_question_cannot_declare_acceptable_evidence() -> None:
    source = copy.deepcopy(_source())
    question = next(item for item in source["questions"] if not item["answerable"])
    question["acceptable_evidence_ids"] = ["REG-001"]

    with pytest.raises(ValueError, match="不可回答问题"):
        build_module.validate_source(source)
