"""Local filesystem blob store with staging/commit semantics."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.settings import get_settings


class BlobStore:
    def __init__(self, root: Path | None = None) -> None:
        s = get_settings()
        self._root = root or Path(s.local_storage_dir)
        self._staging = Path(s.staging_dir)

    @property
    def root(self) -> Path:
        return self._root

    def artifact_dir(self, document_id: str, version: int = 1) -> Path:
        return self._root / "artifacts" / document_id / f"v{version}"

    def job_upload_path(self, job_id: str) -> Path:
        return self._staging / "jobs" / job_id / "upload"

    def put_job_upload(self, job_id: str, data: bytes) -> Path:
        path = self.job_upload_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_job_upload(self, job_id: str) -> bytes:
        return self.job_upload_path(job_id).read_bytes()

    def put_staging(self, staging_key: str, filename: str, data: bytes) -> Path:
        path = self._staging / staging_key / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def commit_bundle(
        self,
        document_id: str,
        *,
        version: int = 1,
        raw_name: str | None,
        raw_bytes: bytes | None,
        extracted_text: str,
        meta: dict[str, Any],
    ) -> str:
        bundle_dir = self.artifact_dir(document_id, version)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        try:
            (bundle_dir / "extracted.txt").write_text(extracted_text, encoding="utf-8")
            (bundle_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if raw_name and raw_bytes is not None:
                (bundle_dir / raw_name).write_bytes(raw_bytes)
            rel = str(bundle_dir.relative_to(self._root)).replace("\\", "/")
            return rel
        except Exception:
            shutil.rmtree(bundle_dir, ignore_errors=True)
            raise

    def delete_prefix(self, prefix: str) -> None:
        target = self._root / prefix if not Path(prefix).is_absolute() else Path(prefix)
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

    def delete_staging(self, staging_key: str) -> None:
        path = self._staging / staging_key
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def delete_job_staging(self, job_id: str) -> None:
        self.delete_staging(f"jobs/{job_id}")

    def read(self, relative_path: str) -> bytes:
        return (self._root / relative_path).read_bytes()

    def read_text(self, relative_path: str) -> str:
        return (self._root / relative_path).read_text(encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        return (self._root / relative_path).exists()

    def list_artifacts(self, document_id: str, version: int = 1) -> list[str]:
        bundle = self.artifact_dir(document_id, version)
        if not bundle.exists():
            return []
        return [p.name for p in bundle.iterdir() if p.is_file()]


def get_blob_store() -> BlobStore:
    s = get_settings()
    if s.minio_enabled:
        from app.stores.minio_blob_store import MinioBlobStore

        return MinioBlobStore()  # type: ignore[return-value]
    return BlobStore()
