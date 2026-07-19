from __future__ import annotations

import pytest

from app.core.generation.citation_validator import (
    parse_answer,
    render_declared_citations,
    validate_citations,
)
from app.core.generation.context_builder import Evidence
from app.core.generation.llm_client import LLMResponseError
from app.domain import AnswerStatus


def _evidence(citation_id: int) -> Evidence:
    return Evidence(
        citation_id=citation_id,
        chunk_id=f"chunk-{citation_id}",
        document_id="doc-1",
        source_uri="upload://guide.pdf",
        original_filename="guide.pdf",
        chunk_index=citation_id - 1,
        page_no=citation_id,
        text=f"证据 {citation_id}",
    )


def test_parse_and_validate_answered_response() -> None:
    parsed = parse_answer(
        '```json\n{"status":"answered","answer":"结论。[2][1]","citation_ids":[2,1]}\n```'
    )

    citations = validate_citations(parsed, [_evidence(1), _evidence(2)])

    assert parsed.status == AnswerStatus.ANSWERED
    assert [item.citation_id for item in citations] == [2, 1]


def test_render_declared_citations_when_answer_body_omits_them() -> None:
    parsed = parse_answer(
        '{"status":"answered","answer":"结论。","citation_ids":[2,1]}'
    )

    rendered = render_declared_citations(parsed, [_evidence(1), _evidence(2)])
    citations = validate_citations(rendered, [_evidence(1), _evidence(2)])

    assert rendered.answer == "结论。 [2][1]"
    assert [item.citation_id for item in citations] == [2, 1]


def test_render_declared_citations_does_not_duplicate_inline_citations() -> None:
    parsed = parse_answer(
        '{"status":"answered","answer":"结论。[1]","citation_ids":[1]}'
    )

    assert render_declared_citations(parsed, [_evidence(1)]) is parsed


def test_render_declared_citations_appends_only_missing_declared_ids() -> None:
    parsed = parse_answer(
        '{"status":"answered","answer":"结论。[2]","citation_ids":[2,1]}'
    )

    rendered = render_declared_citations(parsed, [_evidence(1), _evidence(2)])
    citations = validate_citations(rendered, [_evidence(1), _evidence(2)])

    assert rendered.answer == "结论。[2] [1]"
    assert [item.citation_id for item in citations] == [2, 1]


def test_render_declared_citations_does_not_accept_undeclared_inline_ids() -> None:
    parsed = parse_answer(
        '{"status":"answered","answer":"结论。[2]","citation_ids":[1]}'
    )

    rendered = render_declared_citations(parsed, [_evidence(1), _evidence(2)])

    with pytest.raises(LLMResponseError, match="do not match"):
        validate_citations(rendered, [_evidence(1), _evidence(2)])


@pytest.mark.parametrize(
    "content, message",
    [
        (
            '{"status":"answered","answer":"没有引用","citation_ids":[]}',
            "at least one citation",
        ),
        (
            '{"status":"answered","answer":"未知引用","citation_ids":[9]}',
            "was not provided",
        ),
    ],
)
def test_render_declared_citations_rejects_ungrounded_ids(
    content: str,
    message: str,
) -> None:
    parsed = parse_answer(content)

    with pytest.raises(LLMResponseError, match=message):
        render_declared_citations(parsed, [_evidence(1)])


def test_insufficient_evidence_must_not_claim_citations() -> None:
    parsed = parse_answer(
        '{"status":"insufficient_evidence","answer":"证据不足。","citation_ids":[]}'
    )

    assert validate_citations(parsed, [_evidence(1)]) == []


def test_insufficient_evidence_may_cite_evidence_explaining_refusal() -> None:
    parsed = parse_answer(
        '{"status":"insufficient_evidence",'
        '"answer":"资料只说明了检查重点，没有给出处罚金额。[1]",'
        '"citation_ids":[1]}'
    )

    citations = validate_citations(parsed, [_evidence(1)])

    assert [item.citation_id for item in citations] == [1]


def test_unused_declared_citation_is_ignored_for_uncited_refusal() -> None:
    parsed = parse_answer(
        '{"status":"insufficient_evidence","answer":"证据不足。","citation_ids":[1]}'
    )

    assert validate_citations(parsed, [_evidence(1)]) == []


def test_insufficient_evidence_may_leave_answer_empty_for_service_fallback() -> None:
    parsed = parse_answer('{"status":"insufficient_evidence","answer":"","citation_ids":[]}')

    assert parsed.answer == ""
    assert validate_citations(parsed, [_evidence(1)]) == []


@pytest.mark.parametrize(
    "content, message",
    [
        (
            '{"status":"answered","answer":"没有引用", "citation_ids":[]}',
            "at least one citation",
        ),
        (
            '{"status":"answered","answer":"错误引用。[3]", "citation_ids":[3]}',
            "was not provided",
        ),
        (
            '{"status":"answered","answer":"声明不一致。[1]", "citation_ids":[2]}',
            "do not match",
        ),
    ],
)
def test_invalid_answer_citations_are_rejected(content: str, message: str) -> None:
    parsed = parse_answer(content)
    with pytest.raises(LLMResponseError, match=message):
        validate_citations(parsed, [_evidence(1), _evidence(2)])


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(LLMResponseError, match="invalid answer JSON"):
        parse_answer("这不是 JSON")


def test_answered_response_must_not_be_empty() -> None:
    with pytest.raises(LLMResponseError, match="empty answered response"):
        parse_answer('{"status":"answered","answer":"","citation_ids":[]}')
