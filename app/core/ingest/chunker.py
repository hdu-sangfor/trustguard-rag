"""感知 PDF 页码的文本分块。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.settings import get_settings

PAGE_MARKER = re.compile(r"(?:^|\n)--- Page (\d+) ---\n")


@dataclass
class ChunkDraft:
    text: str
    page_no: int | None
    page_span: str | None
    token_count: int
    metadata: dict[str, Any]


def estimate_tokens(text: str) -> int:
    """用字符数启发式估算词元数量。"""
    return max(1, len(text) // 4)


def _split_paragraphs(text: str) -> list[str]:
    """按空行边界切出非空段落。"""
    parts = re.split(r"\n\s*\n", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _split_oversized(text: str, page_no: int | None, target_tokens: int) -> list[ChunkDraft]:
    """将段落合并成目标大小的分块，并硬切过长段落。"""
    if estimate_tokens(text) <= target_tokens:
        return [
            ChunkDraft(
                text=text,
                page_no=page_no,
                page_span=str(page_no) if page_no else None,
                token_count=estimate_tokens(text),
                metadata={"page_no": page_no, "page_span": str(page_no) if page_no else None},
            )
        ]
    chunks: list[ChunkDraft] = []
    buf: list[str] = []
    buf_tokens = 0
    for para in _split_paragraphs(text):
        pt = estimate_tokens(para)
        if pt > target_tokens:
            if buf:
                joined = "\n\n".join(buf)
                chunks.append(
                    ChunkDraft(
                        text=joined,
                        page_no=page_no,
                        page_span=str(page_no) if page_no else None,
                        token_count=estimate_tokens(joined),
                        metadata={
                            "page_no": page_no,
                            "page_span": str(page_no) if page_no else None,
                        },
                    )
                )
                buf, buf_tokens = [], 0
            step = max(1, target_tokens * 4)
            for i in range(0, len(para), step):
                piece = para[i : i + step].strip()
                if piece:
                    chunks.append(
                        ChunkDraft(
                            text=piece,
                            page_no=page_no,
                            page_span=str(page_no) if page_no else None,
                            token_count=estimate_tokens(piece),
                            metadata={
                                "page_no": page_no,
                                "page_span": str(page_no) if page_no else None,
                            },
                        )
                    )
            continue
        if buf_tokens + pt > target_tokens and buf:
            joined = "\n\n".join(buf)
            chunks.append(
                ChunkDraft(
                    text=joined,
                    page_no=page_no,
                    page_span=str(page_no) if page_no else None,
                    token_count=estimate_tokens(joined),
                    metadata={"page_no": page_no, "page_span": str(page_no) if page_no else None},
                )
            )
            buf, buf_tokens = [], 0
        buf.append(para)
        buf_tokens += pt
    if buf:
        joined = "\n\n".join(buf)
        chunks.append(
            ChunkDraft(
                text=joined,
                page_no=page_no,
                page_span=str(page_no) if page_no else None,
                token_count=estimate_tokens(joined),
                metadata={"page_no": page_no, "page_span": str(page_no) if page_no else None},
            )
        )
    return chunks


def chunk_extracted_text(text: str) -> list[ChunkDraft]:
    """将抽取文本切成可用于嵌入的页码感知分块草稿。"""
    settings = get_settings()
    target = settings.chunk_target_tokens
    chunks: list[ChunkDraft] = []

    parts = PAGE_MARKER.split(text)
    if len(parts) == 1:
        chunks.extend(_split_oversized(text.strip(), None, target))
    else:
        preamble = parts[0].strip()
        if preamble:
            chunks.extend(_split_oversized(preamble, None, target))
        for i in range(1, len(parts), 2):
            if i + 1 >= len(parts):
                break
            page_no = int(parts[i])
            body = parts[i + 1].strip()
            if not body:
                continue
            chunks.extend(_split_oversized(body, page_no, target))

    return [c for c in chunks if c.text.strip()]
