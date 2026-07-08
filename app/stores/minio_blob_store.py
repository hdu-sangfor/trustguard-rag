"""MinIO/S3-compatible blob store with staging/commit semantics."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from minio.deleteobjects import DeleteObject
from minio.error import S3Error

from app.settings import get_settings
from app.stores.minio_client import ensure_bucket, get_minio_client


class MinioBlobStore:
    def __init__(self) -> None:
        s = get_settings()
        self._bucket = s.minio_bucket
        self._client = get_minio_client()

    @property
    def root(self) -> Path:
        # Logical root for path composition; objects live in the bucket.
        return Path(".")

    def _key(self, relative_path: str) -> str:
        return relative_path.replace("\\", "/").lstrip("/")

    def artifact_dir(self, document_id: str, version: int = 1) -> Path:
        return Path("artifacts") / document_id / f"v{version}"

    def job_upload_path(self, job_id: str) -> Path:
        return Path("staging") / "jobs" / job_id / "upload"

    def put_job_upload(self, job_id: str, data: bytes) -> Path:
        key = self._key(str(self.job_upload_path(job_id)))
        self._put_bytes(key, data)
        return Path(key)

    def read_job_upload(self, job_id: str) -> bytes:
        return self.read(str(self.job_upload_path(job_id)))

    def put_staging(self, staging_key: str, filename: str, data: bytes) -> Path:
        key = self._key(f"staging/{staging_key}/{filename}")
        self._put_bytes(key, data)
        return Path(key)

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
        prefix = self._key(str(self.artifact_dir(document_id, version)))
        written: list[str] = []
        try:
            self._put_text(f"{prefix}/extracted.txt", extracted_text)
            written.append(f"{prefix}/extracted.txt")
            self._put_text(
                f"{prefix}/meta.json",
                json.dumps(meta, ensure_ascii=False, indent=2),
            )
            written.append(f"{prefix}/meta.json")
            if raw_name and raw_bytes is not None:
                self._put_bytes(f"{prefix}/{raw_name}", raw_bytes)
                written.append(f"{prefix}/{raw_name}")
            return prefix
        except Exception:
            self.delete_prefix(prefix)
            raise

    def delete_prefix(self, prefix: str) -> None:
        key_prefix = self._key(prefix).rstrip("/") + "/"
        objects = list(
            self._client.list_objects(self._bucket, prefix=key_prefix, recursive=True)
        )
        if not objects:
            single = self._key(prefix)
            try:
                self._client.remove_object(self._bucket, single)
            except S3Error:
                pass
            return
        deletes = [DeleteObject(obj.object_name) for obj in objects]
        errors = self._client.remove_objects(self._bucket, deletes)
        for err in errors:
            raise RuntimeError(f"failed to delete {err.name}: {err}")

    def delete_staging(self, staging_key: str) -> None:
        self.delete_prefix(f"staging/{staging_key}")

    def delete_job_staging(self, job_id: str) -> None:
        self.delete_staging(f"jobs/{job_id}")

    def read(self, relative_path: str) -> bytes:
        key = self._key(relative_path)
        response = self._client.get_object(self._bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def read_text(self, relative_path: str) -> str:
        return self.read(relative_path).decode("utf-8")

    def exists(self, relative_path: str) -> bool:
        key = self._key(relative_path)
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise

    def list_artifacts(self, document_id: str, version: int = 1) -> list[str]:
        prefix = self._key(str(self.artifact_dir(document_id, version))).rstrip("/") + "/"
        names: list[str] = []
        for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=False):
            name = obj.object_name[len(prefix) :]
            if name and "/" not in name:
                names.append(name)
        return sorted(names)

    def artifact_path(self, blob_path: str, filename: str) -> str:
        return self._key(f"{blob_path.rstrip('/')}/{filename}")

    def _put_bytes(self, key: str, data: bytes) -> None:
        ensure_bucket()
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            length=len(data),
        )

    def _put_text(self, key: str, text: str) -> None:
        self._put_bytes(key, text.encode("utf-8"))
