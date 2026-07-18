"""使用模型 tokenizer 的页码感知文本分块。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.settings import Settings, get_settings

PAGE_MARKER = re.compile(r"(?:^|\n)--- Page (\d+) ---\n")

# 优先保留文档结构和中文句子边界，最后才退化为任意字符切分。
CHINESE_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]


class ChunkingError(RuntimeError):
    """分块器无法加载 tokenizer 或生成合法分块时抛出的异常。"""


class TokenCounter(Protocol):
    """分块器所需的最小 tokenizer 计数契约。"""

    def count(self, text: str) -> int:
        """返回文本在目标模型 tokenizer 下的词元数。"""


@dataclass
class ChunkDraft:
    text: str
    page_no: int | None
    page_span: str | None
    token_count: int
    metadata: dict[str, Any]


class HuggingFaceTokenCounter:
    """懒加载并复用 Qwen tokenizer，避免每个分块任务重复初始化。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tokenizer = None

    @lru_cache(maxsize=8192)
    def count(self, text: str) -> int:
        """按不包含模型特殊词元的方式计算正文长度。"""
        if not text:
            return 0
        tokenizer = self._load_tokenizer()
        return len(tokenizer.encode(text, add_special_tokens=False))

    def _load_tokenizer(self):
        """首次计数时加载 tokenizer，并支持现有的模型下载源配置。"""
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise ChunkingError(
                "基于 tokenizer 的分块需要 transformers，请执行 'uv sync'"
            ) from exc

        model_path = self._resolve_model_path()
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                model_path,
                use_fast=True,
                cache_dir=self._settings.embedding_cache_dir,
            )
        except Exception as exc:
            raise ChunkingError(
                f"无法加载分块 tokenizer '{self._settings.chunk_tokenizer_model}': {exc}"
            ) from exc
        return self._tokenizer

    def _resolve_model_path(self) -> str:
        """解析 Hugging Face 名称或由 ModelScope 下载得到的本地路径。"""
        source = self._settings.embedding_download_source.strip().lower()
        if source == "huggingface":
            endpoint = self._settings.huggingface_endpoint or self._settings.huggingface_hub_url
            if endpoint:
                os.environ["HF_ENDPOINT"] = endpoint.rstrip("/")
            if self._settings.embedding_cache_dir:
                os.environ.setdefault("HF_HOME", self._settings.embedding_cache_dir)
            return self._settings.chunk_tokenizer_model
        if source == "modelscope":
            try:
                from modelscope import snapshot_download
            except ImportError as exc:
                raise ChunkingError(
                    "通过 ModelScope 下载 tokenizer 需要 modelscope，"
                    "请执行 'uv sync --extra local-embedding'"
                ) from exc
            if self._settings.modelscope_endpoint:
                os.environ["MODELSCOPE_DOMAIN"] = self._settings.modelscope_endpoint.rstrip("/")
            cache_dir = self._settings.modelscope_cache_dir or self._settings.embedding_cache_dir
            return snapshot_download(
                self._settings.chunk_tokenizer_model,
                cache_dir=cache_dir,
            )
        raise ChunkingError(f"不支持的 tokenizer 下载源：{source}")


_TOKEN_COUNTER_KEY: tuple[Any, ...] | None = None
_TOKEN_COUNTER: HuggingFaceTokenCounter | None = None


def get_token_counter(settings: Settings | None = None) -> HuggingFaceTokenCounter:
    """按 tokenizer 和下载配置缓存本地计数器。"""
    global _TOKEN_COUNTER, _TOKEN_COUNTER_KEY
    current = settings or get_settings()
    key = (
        current.chunk_tokenizer_model,
        current.embedding_download_source,
        current.embedding_cache_dir,
        current.huggingface_endpoint,
        current.huggingface_hub_url,
        current.modelscope_endpoint,
        current.modelscope_cache_dir,
    )
    if _TOKEN_COUNTER is None or _TOKEN_COUNTER_KEY != key:
        _TOKEN_COUNTER = HuggingFaceTokenCounter(current)
        _TOKEN_COUNTER_KEY = key
    return _TOKEN_COUNTER


def count_tokens(text: str) -> int:
    """使用当前配置的本地 tokenizer 返回正文词元数。"""
    return get_token_counter().count(text)


def _split_page(
    text: str,
    page_no: int | None,
    *,
    settings: Settings,
    token_counter: TokenCounter,
) -> list[ChunkDraft]:
    """在单页内按结构边界切分，避免分块跨越 PDF 页码。"""
    if not text.strip():
        return []
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_target_tokens,
        chunk_overlap=settings.chunk_overlap_tokens,
        length_function=token_counter.count,
        separators=CHINESE_SEPARATORS,
        keep_separator=True,
        strip_whitespace=True,
    )
    page_span = str(page_no) if page_no is not None else None
    chunking_metadata = {
        "page_no": page_no,
        "page_span": page_span,
        "chunk_tokenizer_model": settings.chunk_tokenizer_model,
        "chunk_target_tokens": settings.chunk_target_tokens,
        "chunk_overlap_tokens": settings.chunk_overlap_tokens,
    }
    return [
        ChunkDraft(
            text=piece,
            page_no=page_no,
            page_span=page_span,
            token_count=token_counter.count(piece),
            metadata=chunking_metadata.copy(),
        )
        for piece in splitter.split_text(text.strip())
        if piece.strip()
    ]


def chunk_extracted_text(
    text: str,
    *,
    settings: Settings | None = None,
    token_counter: TokenCounter | None = None,
) -> list[ChunkDraft]:
    """按目标模型真实词元长度生成带页码和重叠窗口的分块。"""
    current = settings or get_settings()
    counter = token_counter or get_token_counter(current)
    chunks: list[ChunkDraft] = []

    parts = PAGE_MARKER.split(text)
    if len(parts) == 1:
        return _split_page(text, None, settings=current, token_counter=counter)

    preamble = parts[0].strip()
    if preamble:
        chunks.extend(_split_page(preamble, None, settings=current, token_counter=counter))
    for index in range(1, len(parts), 2):
        if index + 1 >= len(parts):
            break
        page_no = int(parts[index])
        chunks.extend(
            _split_page(
                parts[index + 1],
                page_no,
                settings=current,
                token_counter=counter,
            )
        )
    return chunks
