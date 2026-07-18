"""基于 tokenizer 的中文分块测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.ingest.chunker import HuggingFaceTokenCounter, chunk_extracted_text
from app.settings import Settings


class _CharacterTokenCounter:
    """将单个字符视为一个词元，便于验证确定性的分块边界。"""

    def count(self, text: str) -> int:
        return len(text)


def test_chinese_chunks_use_token_limit_and_overlap() -> None:
    settings = Settings(
        _env_file=None,
        chunk_target_tokens=10,
        chunk_overlap_tokens=2,
    )

    chunks = chunk_extracted_text(
        "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉",
        settings=settings,
        token_counter=_CharacterTokenCounter(),
    )

    assert [chunk.token_count for chunk in chunks] == [10, 10, 4]
    assert chunks[0].text[-2:] == chunks[1].text[:2]
    assert chunks[1].text[-2:] == chunks[2].text[:2]


def test_page_markers_are_not_mixed_across_chunks() -> None:
    settings = Settings(
        _env_file=None,
        chunk_target_tokens=6,
        chunk_overlap_tokens=2,
    )
    text = "--- Page 1 ---\n甲乙丙丁戊己庚辛\n--- Page 2 ---\n壬癸子丑寅卯辰巳"

    chunks = chunk_extracted_text(
        text,
        settings=settings,
        token_counter=_CharacterTokenCounter(),
    )

    assert {chunk.page_no for chunk in chunks} == {1, 2}
    assert all(chunk.metadata["page_span"] == str(chunk.page_no) for chunk in chunks)
    assert all(chunk.token_count <= 6 for chunk in chunks)


def test_huggingface_counter_excludes_special_tokens() -> None:
    calls: list[tuple[str, bool]] = []

    class _Tokenizer:
        def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
            calls.append((text, add_special_tokens))
            return [1, 2, 3]

    counter = HuggingFaceTokenCounter(Settings(_env_file=None))
    counter._tokenizer = _Tokenizer()

    assert counter.count("中文") == 3
    assert calls == [("中文", False)]


@pytest.mark.parametrize(
    ("target", "overlap"),
    [(0, 0), (10, -1), (10, 10), (10, 11)],
)
def test_invalid_chunk_window_is_rejected(target: int, overlap: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            chunk_target_tokens=target,
            chunk_overlap_tokens=overlap,
        )
