"""入库和检索使用的嵌入提供方。

生产路径可使用本地 Qwen3-Embedding 或 OpenAI 兼容 API。
测试和离线冒烟运行仍可使用确定性的伪向量。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingUsage:
    """一次嵌入操作在远程提供方产生的准确调用级用量。"""

    prompt_tokens: int
    total_tokens: int
    request_count: int


@dataclass(frozen=True)
class EmbeddingBatchResult:
    """嵌入向量及其可选的远程 API 汇总用量。"""

    vectors: list[list[float]]
    usage: EmbeddingUsage | None = None


class EmbeddingError(RuntimeError):
    """嵌入提供方无法产出有效向量时抛出的异常。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


def _provider_batch_limit(error_text: str) -> int | None:
    """从兼容 API 的错误信息中提取提供方声明的批量上限。"""
    if "batch size" not in error_text.lower():
        return None
    match = re.search(r"not be larger than\s+(\d+)", error_text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _pseudo_vector(text: str, dim: int) -> list[float]:
    """为测试和轻依赖开发环境返回确定性的归一化向量。"""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    idx = 0
    while len(out) < dim:
        block = hashlib.sha256(seed + bytes([idx % 256])).digest()
        for b in block:
            out.append((b / 127.5) - 1.0)
            if len(out) >= dim:
                break
        idx += 1
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


def normalize_embedding_provider(provider: str) -> str:
    """将外部配置的提供方名称归一化为内部分发值。"""
    value = provider.strip().lower()
    if value in {"api", "openai", "openai_compatible", "remote"}:
        return "api"
    if value in {"local", "huggingface", "hf", "modelscope"}:
        return "local"
    if value in {"pseudo", "mock", "fake"}:
        return "pseudo"
    raise EmbeddingError(f"Unsupported embedding provider: {provider}")


def _validate_vectors(vectors: list[list[float]], expected_dim: int) -> list[list[float]]:
    """校验嵌入向量维度是否与配置一致。"""
    for i, vector in enumerate(vectors):
        if len(vector) != expected_dim:
            raise EmbeddingError(
                f"Embedding dimension mismatch at index {i}: "
                f"expected {expected_dim}, got {len(vector)}"
            )
    return vectors


class EmbeddingClient:
    """将嵌入请求分发到本地、API 或伪向量提供方的门面。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化嵌入客户端并缓存归一化后的提供方类型。"""
        self._settings = settings or get_settings()
        self._provider = normalize_embedding_provider(self._settings.embedding_provider)

    @property
    def model_name(self) -> str:
        """返回当前配置的嵌入模型名称。"""
        return self._settings.embedding_model

    @property
    def dimension(self) -> int:
        """返回当前配置的嵌入向量维度。"""
        return self._settings.embedding_dim

    async def embed_query(self, text: str) -> list[float]:
        """按配置的查询指令嵌入单条查询文本。"""
        return (await self._embed_with_usage([text], is_query=True)).vectors[0]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """按输入顺序嵌入文档分块文本。"""
        return (await self.embed_texts_with_usage(texts)).vectors

    async def embed_texts_with_usage(self, texts: list[str]) -> EmbeddingBatchResult:
        """嵌入文档分块，并返回提供方给出的批次级准确用量。"""
        return await self._embed_with_usage(texts, is_query=False)

    async def _embed_with_usage(self, texts: list[str], *, is_query: bool) -> EmbeddingBatchResult:
        """根据提供方类型执行实际嵌入，并统一校验向量维度。"""
        if not texts:
            return EmbeddingBatchResult(vectors=[])
        if self._provider == "api":
            result = await self._remote_embed(texts, is_query=is_query)
        elif self._provider == "local":
            result = EmbeddingBatchResult(vectors=await self._local_embed(texts, is_query=is_query))
        else:
            result = EmbeddingBatchResult(
                vectors=[_pseudo_vector(t, self._settings.embedding_dim) for t in texts]
            )
        return EmbeddingBatchResult(
            vectors=_validate_vectors(result.vectors, self._settings.embedding_dim),
            usage=result.usage,
        )

    async def _remote_embed(self, texts: list[str], *, is_query: bool) -> EmbeddingBatchResult:
        """调用兼容 OpenAI 协议的嵌入接口。"""
        if not self._settings.embedding_base_url:
            raise EmbeddingError("RAG_EMBEDDING_BASE_URL is required for API embeddings")
        url = f"{self._settings.embedding_base_url.rstrip('/')}/embeddings"
        headers = {}
        if self._settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {self._settings.embedding_api_key}"
        prepared = self._prepare_texts(texts, is_query=is_query)
        batch_size = max(1, self._settings.embedding_batch_size)
        vectors: list[list[float]] = []
        prompt_tokens = 0
        total_tokens = 0
        usage_seen = False
        request_count = 0
        async with httpx.AsyncClient(
            timeout=self._settings.embedding_api_timeout_seconds
        ) as client:
            start = 0
            while start < len(prepared):
                batch = prepared[start : start + batch_size]
                payload: dict[str, Any] = {
                    "model": self._settings.embedding_model,
                    "input": batch,
                }
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    provider_limit = _provider_batch_limit(resp.text)
                    if (
                        resp.status_code == 400
                        and len(batch) > 1
                        and provider_limit is not None
                        and provider_limit < len(batch)
                    ):
                        batch_size = max(1, min(batch_size, provider_limit))
                        continue
                    retryable = resp.status_code == 429 or resp.status_code >= 500
                    raise EmbeddingError(
                        f"Embedding API request failed: {resp.status_code} {resp.text}",
                        retryable=retryable,
                    ) from e
                except httpx.RequestError as e:
                    raise EmbeddingError(
                        f"Embedding API request failed: {e}",
                        retryable=True,
                    ) from e
                try:
                    body = resp.json()
                    data = body["data"]
                    batch_vectors = [
                        item["embedding"] for item in sorted(data, key=lambda x: x["index"])
                    ]
                except (KeyError, TypeError, ValueError) as e:
                    raise EmbeddingError("Embedding API returned an invalid response") from e
                if len(batch_vectors) != len(payload["input"]):
                    raise EmbeddingError(
                        "Embedding API returned a different number of vectors than inputs"
                    )
                vectors.extend(batch_vectors)
                request_count += 1
                usage = body.get("usage")
                if isinstance(usage, dict):
                    batch_prompt_tokens = usage.get("prompt_tokens")
                    batch_total_tokens = usage.get("total_tokens")
                    if isinstance(batch_prompt_tokens, int) and batch_prompt_tokens >= 0:
                        prompt_tokens += batch_prompt_tokens
                        usage_seen = True
                    if isinstance(batch_total_tokens, int) and batch_total_tokens >= 0:
                        total_tokens += batch_total_tokens
                        usage_seen = True
                start += len(batch)
        aggregate_usage = (
            EmbeddingUsage(
                prompt_tokens=prompt_tokens,
                total_tokens=total_tokens,
                request_count=request_count,
            )
            if usage_seen
            else None
        )
        if aggregate_usage is not None:
            logger.info(
                "嵌入 API 用量：model=%s inputs=%d requests=%d prompt_tokens=%d total_tokens=%d",
                self._settings.embedding_model,
                len(texts),
                aggregate_usage.request_count,
                aggregate_usage.prompt_tokens,
                aggregate_usage.total_tokens,
            )
        return EmbeddingBatchResult(vectors=vectors, usage=aggregate_usage)

    async def _local_embed(self, texts: list[str], *, is_query: bool) -> list[list[float]]:
        """在线程池中调用本地 Sentence Transformers 模型。"""
        provider = _get_local_provider(self._settings)
        return await asyncio.to_thread(provider.encode, texts, is_query)

    def _prepare_texts(self, texts: list[str], *, is_query: bool) -> list[str]:
        """在查询嵌入场景下为文本追加模型需要的查询指令。"""
        if not is_query or not self._settings.embedding_query_instruction:
            return texts
        instruction = self._settings.embedding_query_instruction.strip()
        return [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


class LocalSentenceTransformerProvider:
    """支持 Hugging Face/ModelScope 下载的懒加载本地模型提供方。"""

    def __init__(self, settings: Settings) -> None:
        """保存配置并延迟加载实际模型。"""
        self._settings = settings
        self._model = None

    def encode(self, texts: list[str], is_query: bool) -> list[list[float]]:
        """使用本地模型编码文本并返回普通的 Python 向量列表。"""
        model = self._load_model()
        prepared = self._prepare_texts(texts, is_query=is_query)
        vectors = model.encode(
            prepared,
            batch_size=self._settings.embedding_batch_size,
            normalize_embeddings=self._settings.embedding_normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    def _load_model(self):
        """首次使用时加载 Sentence Transformers 模型并复用实例。"""
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise EmbeddingError(
                "Local embeddings require sentence-transformers. "
                "Run 'uv sync --extra local-embedding' or switch "
                "RAG_EMBEDDING_PROVIDER=api/pseudo."
            ) from e

        model_path = self._resolve_model_path()
        kwargs: dict[str, Any] = {}
        device = self._settings.embedding_device
        if device and device != "auto":
            kwargs["device"] = device
        if self._settings.embedding_cache_dir:
            kwargs["cache_folder"] = self._settings.embedding_cache_dir
        self._model = SentenceTransformer(model_path, **kwargs)
        return self._model

    def _resolve_model_path(self) -> str:
        """根据下载源配置解析本地模型路径或远程模型名称。"""
        source = self._settings.embedding_download_source.strip().lower()
        if source == "modelscope":
            return self._download_from_modelscope()
        if source != "huggingface":
            raise EmbeddingError(f"Unsupported embedding download source: {source}")
        self._configure_huggingface_env()
        return self._settings.embedding_model

    def _configure_huggingface_env(self) -> None:
        """按配置设置 Hugging Face 下载端点和缓存目录环境变量。"""
        endpoint = self._settings.huggingface_endpoint or self._settings.huggingface_hub_url
        if endpoint:
            os.environ["HF_ENDPOINT"] = endpoint.rstrip("/")
        if self._settings.embedding_cache_dir:
            os.environ.setdefault("HF_HOME", self._settings.embedding_cache_dir)

    def _download_from_modelscope(self) -> str:
        """通过 ModelScope 下载模型并返回本地快照路径。"""
        try:
            from modelscope import snapshot_download
        except ImportError as e:
            raise EmbeddingError(
                "ModelScope downloads require modelscope. "
                "Run 'uv sync --extra local-embedding' or set "
                "RAG_EMBEDDING_DOWNLOAD_SOURCE=huggingface."
            ) from e
        if self._settings.modelscope_endpoint:
            os.environ["MODELSCOPE_DOMAIN"] = self._settings.modelscope_endpoint.rstrip("/")
        cache_dir = self._settings.modelscope_cache_dir or self._settings.embedding_cache_dir
        return snapshot_download(self._settings.embedding_model, cache_dir=cache_dir)

    def _prepare_texts(self, texts: list[str], *, is_query: bool) -> list[str]:
        """在查询嵌入场景下为本地模型输入追加查询指令。"""
        if not is_query or not self._settings.embedding_query_instruction:
            return texts
        instruction = self._settings.embedding_query_instruction.strip()
        return [f"Instruct: {instruction}\nQuery: {text}" for text in texts]


_LOCAL_PROVIDER_KEY: tuple[Any, ...] | None = None
_LOCAL_PROVIDER: LocalSentenceTransformerProvider | None = None


def _get_local_provider(settings: Settings) -> LocalSentenceTransformerProvider:
    """按关键配置缓存并复用本地模型提供方。"""
    global _LOCAL_PROVIDER, _LOCAL_PROVIDER_KEY
    key = (
        settings.embedding_model,
        settings.embedding_device,
        settings.embedding_download_source,
        settings.embedding_cache_dir,
        settings.huggingface_endpoint,
        settings.huggingface_hub_url,
        settings.modelscope_endpoint,
        settings.modelscope_cache_dir,
        settings.embedding_batch_size,
        settings.embedding_normalize,
        settings.embedding_query_instruction,
    )
    if _LOCAL_PROVIDER is None or _LOCAL_PROVIDER_KEY != key:
        _LOCAL_PROVIDER = LocalSentenceTransformerProvider(settings)
        _LOCAL_PROVIDER_KEY = key
    return _LOCAL_PROVIDER


def get_embedding_client() -> EmbeddingClient:
    """根据当前应用配置创建嵌入客户端。"""
    return EmbeddingClient()
