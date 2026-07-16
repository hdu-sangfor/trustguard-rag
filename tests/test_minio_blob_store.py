"""使用模拟客户端的 MinIO 对象存储单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from minio.error import S3Error

from app.settings import get_settings


@pytest.fixture
def minio_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_MINIO_ENABLED", "true")
    monkeypatch.setenv("RAG_MINIO_ENDPOINT", "127.0.0.1:18217")
    get_settings.cache_clear()


def test_get_blob_store_uses_minio_when_enabled(minio_env: None) -> None:
    from app.stores.blob_store import get_blob_store
    from app.stores.minio_blob_store import MinioBlobStore

    with patch("app.stores.minio_blob_store.get_minio_client") as mock_client:
        mock_client.return_value = MagicMock()
        store = get_blob_store()
    assert isinstance(store, MinioBlobStore)


def test_minio_commit_bundle_writes_objects(minio_env: None) -> None:
    from app.stores.minio_blob_store import MinioBlobStore

    client = MagicMock()
    with (
        patch("app.stores.minio_blob_store.get_minio_client", return_value=client),
        patch("app.stores.minio_blob_store.ensure_bucket"),
    ):
        store = MinioBlobStore()
        prefix = store.commit_bundle(
            "doc-1",
            raw_name="raw.pdf",
            raw_bytes=b"%PDF",
            extracted_text="hello",
            meta={"pages": 1},
        )

    assert prefix == "artifacts/doc-1/v1"
    assert client.put_object.call_count == 3


def test_minio_delete_prefix_propagates_access_denied(minio_env: None) -> None:
    from app.stores.minio_blob_store import MinioBlobStore

    client = MagicMock()
    client.list_objects.return_value = []
    client.remove_object.side_effect = S3Error(
        MagicMock(), "AccessDenied", "denied", "artifacts/doc-1", "request", "host"
    )
    with patch("app.stores.minio_blob_store.get_minio_client", return_value=client):
        store = MinioBlobStore()

    with pytest.raises(S3Error):
        store.delete_prefix("artifacts/doc-1")
