"""将排序后的检索结果组装为受 Token 预算约束的证据上下文。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Protocol

from app.core.ingest import chunker
from app.settings import Settings, get_settings


class TokenCounter(Protocol):
    def count(self, text: str) -> int:
        """返回文本的 Token 数量。"""


@dataclass(frozen=True)
class Evidence:
    """提供给模型并可被答案引用的单条证据。"""

    citation_id: int
    chunk_id: str
    document_id: str
    source_uri: str
    original_filename: str | None
    chunk_index: int
    page_no: int | None
    text: str
    truncated: bool = False

    def as_prompt_value(self) -> dict[str, Any]:
        """转换为模型可读、边界明确的 JSON 值。"""
        return {
            "citation_id": self.citation_id,
            "source": {
                "chunk_id": self.chunk_id,
                "document_id": self.document_id,
                "source_uri": self.source_uri,
                "original_filename": self.original_filename,
                "chunk_index": self.chunk_index,
                "page_no": self.page_no,
            },
            "content": self.text,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ContextBundle:
    """最终上下文及其引用映射。"""

    context: str
    evidence: list[Evidence]
    token_count: int


class ContextBuilder:
    """按检索顺序去重、截断并序列化证据。"""

    def __init__(
        self,
        settings: Settings | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._token_counter = token_counter or chunker.get_token_counter(self._settings)

    def build(self, results: list[dict[str, Any]]) -> ContextBundle:
        """构建不超过配置预算的 JSON 证据数组。"""
        selected: list[Evidence] = []
        seen_chunk_ids: set[str] = set()

        for item in results:
            if len(selected) >= self._settings.answer_max_context_chunks:
                break
            chunk_id = str(item.get("chunk_id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not chunk_id or not text or chunk_id in seen_chunk_ids:
                continue

            evidence = self._to_evidence(item, len(selected) + 1, text)
            if self._fits([*selected, evidence]):
                selected.append(evidence)
                seen_chunk_ids.add(chunk_id)
                continue

            truncated = self._truncate_to_fit(selected, evidence)
            if truncated is not None:
                selected.append(truncated)
                seen_chunk_ids.add(chunk_id)
                # 截断后的最高优先级证据已经占满剩余预算。
                break
            # 极长的来源元数据也可能导致单条证据无法容纳；继续尝试后续候选。

        context = self._serialize(selected)
        return ContextBundle(
            context=context,
            evidence=selected,
            token_count=self._token_counter.count(context),
        )

    def _to_evidence(self, item: dict[str, Any], citation_id: int, text: str) -> Evidence:
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        return Evidence(
            citation_id=citation_id,
            chunk_id=str(item.get("chunk_id") or ""),
            document_id=str(source.get("document_id") or ""),
            source_uri=str(source.get("source_uri") or ""),
            original_filename=source.get("original_filename"),
            chunk_index=int(source.get("chunk_index") or 0),
            page_no=source.get("page_no"),
            text=text,
        )

    def _truncate_to_fit(self, selected: list[Evidence], evidence: Evidence) -> Evidence | None:
        low = 0
        high = len(evidence.text)
        best: Evidence | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate_text = evidence.text[:middle].rstrip()
            candidate = replace(
                evidence,
                text=candidate_text,
                truncated=middle < len(evidence.text),
            )
            if candidate_text and self._fits([*selected, candidate]):
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _fits(self, evidence: list[Evidence]) -> bool:
        return (
            self._token_counter.count(self._serialize(evidence))
            <= self._settings.answer_context_max_tokens
        )

    @staticmethod
    def _serialize(evidence: list[Evidence]) -> str:
        return json.dumps(
            [item.as_prompt_value() for item in evidence],
            ensure_ascii=False,
            separators=(",", ":"),
        )
