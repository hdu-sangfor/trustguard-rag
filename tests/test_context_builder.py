from __future__ import annotations

import json

from app.core.generation.context_builder import ContextBuilder
from app.settings import Settings


class _CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


def _result(chunk_id: str, text: str, index: int = 0) -> dict:
    return {
        "chunk_id": chunk_id,
        "text": text,
        "source": {
            "document_id": "doc-1",
            "source_uri": "upload://guide.pdf",
            "original_filename": "guide.pdf",
            "chunk_index": index,
            "page_no": index + 1,
        },
    }


def test_context_builder_deduplicates_and_numbers_evidence() -> None:
    settings = Settings(
        _env_file=None,
        answer_context_max_tokens=2000,
        answer_max_context_chunks=3,
    )
    builder = ContextBuilder(settings, token_counter=_CharacterCounter())

    bundle = builder.build(
        [
            _result("chunk-1", "第一条证据"),
            _result("chunk-1", "重复证据"),
            _result("chunk-2", "第二条证据", 1),
        ]
    )

    assert [item.citation_id for item in bundle.evidence] == [1, 2]
    assert [item.chunk_id for item in bundle.evidence] == ["chunk-1", "chunk-2"]
    serialized = json.loads(bundle.context)
    assert serialized[0]["source"]["page_no"] == 1
    assert serialized[1]["citation_id"] == 2
    assert bundle.token_count == len(bundle.context)


def test_context_builder_truncates_to_budget() -> None:
    settings = Settings(
        _env_file=None,
        answer_context_max_tokens=260,
        answer_max_context_chunks=3,
    )
    builder = ContextBuilder(settings, token_counter=_CharacterCounter())

    bundle = builder.build([_result("chunk-1", "证据" * 200)])

    assert len(bundle.evidence) == 1
    assert bundle.evidence[0].truncated is True
    assert len(bundle.evidence[0].text) < 400
    assert bundle.token_count <= 260


def test_context_builder_skips_empty_results_and_honors_chunk_limit() -> None:
    settings = Settings(
        _env_file=None,
        answer_context_max_tokens=2000,
        answer_max_context_chunks=1,
    )
    builder = ContextBuilder(settings, token_counter=_CharacterCounter())

    bundle = builder.build(
        [
            _result("", "没有 ID"),
            _result("chunk-1", ""),
            _result("chunk-2", "有效证据"),
            _result("chunk-3", "不会加入"),
        ]
    )

    assert [item.chunk_id for item in bundle.evidence] == ["chunk-2"]
