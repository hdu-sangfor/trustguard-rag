"""Artifact 删除失败必须阻止元数据清理。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.stores.blob_store import BlobStore


def test_local_delete_prefix_raises_when_directory_remains(tmp_path) -> None:
    store = BlobStore(root=tmp_path)
    target = tmp_path / "artifacts" / "doc-1"
    target.mkdir(parents=True)
    (target / "artifact.txt").write_text("data", encoding="utf-8")

    with patch("app.stores.blob_store.shutil.rmtree"):
        with pytest.raises(RuntimeError, match="artifact path still exists"):
            store.delete_prefix("artifacts/doc-1")
