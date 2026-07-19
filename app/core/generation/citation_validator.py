"""解析并校验模型输出的答案状态与引用。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.core.generation.context_builder import Evidence
from app.core.generation.llm_client import LLMResponseError
from app.domain import AnswerStatus

CITATION_PATTERN = re.compile(r"\[(\d+)]")


@dataclass(frozen=True)
class ParsedAnswer:
    status: AnswerStatus
    answer: str
    citation_ids: list[int]


def parse_answer(content: str) -> ParsedAnswer:
    """从兼容纯 JSON 或 Markdown 围栏的模型输出中解析回答。"""
    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\s*```$", "", normalized)
    try:
        value = json.loads(normalized)
        status = AnswerStatus(value["status"])
        answer = value["answer"]
        citation_ids = value["citation_ids"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise LLMResponseError("LLM returned invalid answer JSON") from exc

    if not isinstance(answer, str):
        raise LLMResponseError("LLM returned an invalid answer")
    if status == AnswerStatus.ANSWERED and not answer.strip():
        raise LLMResponseError("LLM returned an empty answered response")
    if (
        not isinstance(citation_ids, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in citation_ids)
        or len(citation_ids) != len(set(citation_ids))
    ):
        raise LLMResponseError("LLM returned invalid citation_ids")
    return ParsedAnswer(
        status=status,
        answer=answer.strip(),
        citation_ids=citation_ids,
    )


def render_declared_citations(
    parsed: ParsedAnswer,
    evidence: list[Evidence],
) -> ParsedAnswer:
    """用经过校验的结构化编号补齐 answered 正文中遗漏的引用标记。"""
    if parsed.status != AnswerStatus.ANSWERED:
        return parsed

    if not parsed.citation_ids:
        raise LLMResponseError(
            "Answered response must contain at least one citation or declare citation_ids"
        )

    evidence_ids = {item.citation_id for item in evidence}
    if any(citation_id not in evidence_ids for citation_id in parsed.citation_ids):
        raise LLMResponseError("Answer references evidence that was not provided")

    referenced = {int(value) for value in CITATION_PATTERN.findall(parsed.answer)}
    # 正文里出现未声明编号时不做猜测，交给严格校验返回 502。这里只补结构化
    # citation_ids 已声明但正文遗漏的编号。
    if not referenced.issubset(parsed.citation_ids):
        return parsed

    missing = [citation_id for citation_id in parsed.citation_ids if citation_id not in referenced]
    if not missing:
        return parsed

    rendered = "".join(f"[{citation_id}]" for citation_id in missing)
    return ParsedAnswer(
        status=parsed.status,
        answer=f"{parsed.answer.rstrip()} {rendered}",
        citation_ids=parsed.citation_ids,
    )


def validate_citations(parsed: ParsedAnswer, evidence: list[Evidence]) -> list[Evidence]:
    """确保声明编号、正文编号和真实证据严格一致。"""
    referenced = [int(value) for value in CITATION_PATTERN.findall(parsed.answer)]
    referenced_unique = list(dict.fromkeys(referenced))

    if parsed.status == AnswerStatus.INSUFFICIENT_EVIDENCE:
        # 拒答可以引用“现有资料只说明了什么、但缺少什么”的证据。没有在正文中
        # 实际使用的声明编号不对外返回，也不应让一次正确拒答变成 502。
        if not referenced_unique:
            return []

    if not referenced_unique:
        raise LLMResponseError("Answered response must contain at least one citation")
    if set(referenced_unique) != set(parsed.citation_ids):
        raise LLMResponseError("Answer citations do not match declared citation_ids")

    by_id = {item.citation_id: item for item in evidence}
    if any(citation_id not in by_id for citation_id in referenced_unique):
        raise LLMResponseError("Answer references evidence that was not provided")
    return [by_id[citation_id] for citation_id in referenced_unique]
