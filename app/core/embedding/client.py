"""Embedding client with pseudo-vector fallback."""
from __future__ import annotations

import hashlib
import math

import httpx

from app.settings import get_settings


def _pseudo_vector(text: str, dim: int) -> list[float]:
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


class EmbeddingClient:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        if not texts:
            return []
        if settings.embedding_base_url and settings.embedding_api_key:
            return await self._remote_embed(texts)
        return [_pseudo_vector(t, settings.embedding_dim) for t in texts]

    async def _remote_embed(self, texts: list[str]) -> list[list[float]]:
        settings = get_settings()
        url = f"{settings.embedding_base_url.rstrip('/')}/embeddings"
        headers = {"Authorization": f"Bearer {settings.embedding_api_key}"}
        payload = {"model": settings.embedding_model, "input": texts}
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()["data"]
            return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]


def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient()
