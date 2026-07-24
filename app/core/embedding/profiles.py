"""入库和检索可选择的向量化模型白名单。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.settings import Settings, get_settings

_PROFILE_ALIASES = {
    "qwen3.7-text-embedding-native-2560": "qwen3.7-text-embedding-2560",
    "text-embedding-v4-native-2048": "text-embedding-v4-2048",
}


def canonical_embedding_profile_id(profile_id: str | None) -> str:
    requested = (profile_id or "configured").strip()
    return _PROFILE_ALIASES.get(requested, requested)


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    id: str
    label: str
    provider: str
    model: str
    dimension: int
    query_instruction: str
    retrieval_min_score: float
    api_driver: str = "openai_compatible"
    available: bool = True
    unavailable_reason: str | None = None
    legacy_collection: bool = False

    def public_dict(self, *, default: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "api_driver": self.api_driver if self.provider == "api" else None,
            "model": self.model,
            "dimension": self.dimension,
            "retrieval_min_score": self.retrieval_min_score,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "default": default,
        }


def list_embedding_profiles(settings: Settings | None = None) -> list[EmbeddingProfile]:
    settings = settings or get_settings()
    instruction = (
        "Given a cybersecurity search query, retrieve relevant passages that "
        "answer the query"
    )
    api_available = bool(settings.embedding_base_url)
    api_reason = None if api_available else "未配置 RAG_EMBEDDING_BASE_URL"
    configured_provider = settings.embedding_provider.strip().lower()
    configured_driver = settings.embedding_api_driver
    if configured_provider in {"bailian", "dashscope", "aliyun"}:
        configured_provider = "api"
        configured_driver = "bailian"
    elif configured_provider in {"api", "openai", "openai_compatible", "remote"}:
        configured_provider = "api"
    elif configured_provider in {"local", "huggingface", "hf", "modelscope"}:
        configured_provider = "local"
    elif configured_provider in {"pseudo", "mock", "fake"}:
        configured_provider = "pseudo"
    return [
        EmbeddingProfile(
            id="configured",
            label=f"当前配置 · {settings.embedding_model}",
            provider=configured_provider,
            model=settings.embedding_model,
            dimension=settings.embedding_dim,
            query_instruction=settings.embedding_query_instruction,
            retrieval_min_score=_retrieval_min_score(
                settings.embedding_model,
                settings.embedding_dim,
            ),
            api_driver=configured_driver,
            legacy_collection=True,
        ),
        EmbeddingProfile(
            id="qwen3-embedding-0.6b",
            label="Qwen3 Embedding 0.6B · 本地",
            provider="local",
            model="Qwen/Qwen3-Embedding-0.6B",
            dimension=1024,
            query_instruction=instruction,
            retrieval_min_score=0.52,
        ),
        EmbeddingProfile(
            id="bge-m3",
            label="BGE-M3 · 本地",
            provider="local",
            model="BAAI/bge-m3",
            dimension=1024,
            query_instruction="",
            retrieval_min_score=0.575,
        ),
        EmbeddingProfile(
            id="qwen3.7-text-embedding",
            label="百炼 qwen3.7-text-embedding · 1024 维",
            provider="api",
            model="qwen3.7-text-embedding",
            dimension=1024,
            query_instruction=instruction,
            retrieval_min_score=0.60,
            api_driver="bailian",
            available=api_available,
            unavailable_reason=api_reason,
        ),
        EmbeddingProfile(
            id="qwen3.7-text-embedding-2560",
            label="百炼 qwen3.7-text-embedding · 2560 维",
            provider="api",
            model="qwen3.7-text-embedding",
            dimension=2560,
            query_instruction=instruction,
            retrieval_min_score=0.575,
            api_driver="bailian",
            available=api_available,
            unavailable_reason=api_reason,
        ),
        EmbeddingProfile(
            id="text-embedding-v4",
            label="百炼 text-embedding-v4 · 1024 维",
            provider="api",
            model="text-embedding-v4",
            dimension=1024,
            query_instruction=instruction,
            retrieval_min_score=0.58,
            api_driver="bailian",
            available=api_available,
            unavailable_reason=api_reason,
        ),
        EmbeddingProfile(
            id="text-embedding-v4-2048",
            label="百炼 text-embedding-v4 · 2048 维",
            provider="api",
            model="text-embedding-v4",
            dimension=2048,
            query_instruction=instruction,
            retrieval_min_score=0.575,
            api_driver="bailian",
            available=api_available,
            unavailable_reason=api_reason,
        ),
    ]


def _retrieval_min_score(model: str, dimension: int) -> float:
    """返回由 crawler V2 校准的保守拒答阈值，未知模型使用较低默认值。"""
    normalized = model.strip().casefold()
    if "qwen3.7-text-embedding" in normalized:
        return 0.575 if dimension >= 2560 else 0.60
    if "qwen3-embedding" in normalized:
        return 0.52
    if "bge-m3" in normalized:
        return 0.575
    if "text-embedding-v4" in normalized:
        return 0.575 if dimension >= 2048 else 0.58
    return 0.50


def get_embedding_profile(
    profile_id: str | None, settings: Settings | None = None
) -> EmbeddingProfile:
    settings = settings or get_settings()
    requested = canonical_embedding_profile_id(profile_id)
    for profile in list_embedding_profiles(settings):
        if profile.id == requested:
            if not profile.available:
                raise ValueError(profile.unavailable_reason or "Embedding profile unavailable")
            return profile
    raise ValueError(f"Unsupported embedding profile: {requested}")


def profile_settings(
    profile: EmbeddingProfile, settings: Settings | None = None
) -> Settings:
    settings = settings or get_settings()
    return settings.model_copy(
        update={
            "embedding_provider": profile.provider,
            "embedding_api_driver": profile.api_driver,
            "embedding_model": profile.model,
            "embedding_dim": profile.dimension,
            "embedding_query_instruction": profile.query_instruction,
        }
    )


def collection_name(
    profile: EmbeddingProfile, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    base = f"{settings.qdrant_collection_prefix}chunks"
    suffix = profile.id.replace(".", "_")
    return base if profile.legacy_collection else f"{base}__{suffix}"
