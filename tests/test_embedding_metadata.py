"""嵌入元数据测试。"""
from __future__ import annotations

import pytest

from app.core.ingest.pipeline import _embedding_metadata
from app.settings import Settings


@pytest.mark.parametrize("provider", ["api", "openai_compatible", "pseudo"])
def test_non_local_embedding_metadata_omits_download_source(provider: str) -> None:
    metadata = _embedding_metadata(
        Settings(
            embedding_provider=provider,
            embedding_download_source="huggingface",
        )
    )

    assert "embedding_download_source" not in metadata


def test_local_embedding_metadata_includes_download_source() -> None:
    metadata = _embedding_metadata(
        Settings(
            embedding_provider="local",
            embedding_download_source="modelscope",
        )
    )

    assert metadata == {
        "embedding_provider": "local",
        "embedding_download_source": "modelscope",
    }
