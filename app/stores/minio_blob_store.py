"""带暂存和提交语义的 MinIO/S3 兼容对象存储。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from minio.deleteobjects import DeleteObject
from minio.error import S3Error

from app.settings import get_settings
from app.stores.minio_client import ensure_bucket, get_minio_client

_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchObject", "NotFound"})


class MinioBlobStore:
    def __init__(self) -> None:
        """初始化 S3 兼容客户端和目标存储桶名称。"""
        s = get_settings()
        self._bucket = s.minio_bucket
        self._client = get_minio_client()

    @property
    def root(self) -> Path:
        """返回逻辑根目录，以复用本地存储接口。"""
        # 用于路径拼接的逻辑根目录；真实对象存放在存储桶中。
        return Path(".")

    def _key(self, relative_path: str) -> str:
        """将相对路径安全地规范化为对象存储键。"""
        key = relative_path.replace("\\", "/").lstrip("/")
        if not key or ".." in key.split("/"):
            raise ValueError(f"invalid blob path: {relative_path}")
        return key

    def artifact_dir(self, document_id: str, version: int = 1) -> Path:
        """返回某个文档版本的逻辑产物前缀。"""
        return Path("artifacts") / document_id / f"v{version}"

    def job_upload_path(self, job_id: str) -> Path:
        """返回任务上传文件的逻辑暂存键。"""
        return Path("staging") / "jobs" / job_id / "upload"

    def put_job_upload(self, job_id: str, data: bytes) -> Path:
        """将上传文件保存到 MinIO 支持的任务暂存区。"""
        key = self._key(str(self.job_upload_path(job_id)))
        self._put_bytes(key, data)
        return Path(key)

    def read_job_upload(self, job_id: str) -> bytes:
        """从 MinIO 支持的任务暂存区读取上传文件字节。"""
        return self.read(str(self.job_upload_path(job_id)))

    def put_staging(self, staging_key: str, filename: str, data: bytes) -> Path:
        """在暂存前缀下写入任意暂存对象。"""
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
        """在同一对象前缀下提交原始文件、抽取文本和元数据产物。"""
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
        """删除前缀下所有对象；没有子对象时尝试删除单个对象。"""
        key_prefix = self._key(prefix).rstrip("/") + "/"
        objects = list(self._client.list_objects(self._bucket, prefix=key_prefix, recursive=True))
        if not objects:
            single = self._key(prefix)
            try:
                self._client.remove_object(self._bucket, single)
            except S3Error as exc:
                if exc.code not in _NOT_FOUND_CODES:
                    raise
            return
        deletes = [DeleteObject(obj.object_name) for obj in objects]
        errors = self._client.remove_objects(self._bucket, deletes)
        for err in errors:
            raise RuntimeError(f"failed to delete {err.name}: {err}")
        remaining = list(self._client.list_objects(self._bucket, prefix=key_prefix, recursive=True))
        if remaining:
            names = ", ".join(obj.object_name for obj in remaining[:3])
            raise RuntimeError(f"artifact objects still exist after deletion: {names}")

    def delete_staging(self, staging_key: str) -> None:
        """在任务进入终态后删除对应暂存子树。"""
        self.delete_prefix(f"staging/{staging_key}")

    def delete_job_staging(self, job_id: str) -> None:
        """删除单个入库任务的所有 MinIO 暂存对象。"""
        self.delete_staging(f"jobs/{job_id}")

    def read(self, relative_path: str) -> bytes:
        """按逻辑路径读取对象字节。"""
        key = self._key(relative_path)
        response = self._client.get_object(self._bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def read_text(self, relative_path: str) -> str:
        """读取对象并按 UTF-8 解码为文本。"""
        return self.read(relative_path).decode("utf-8")

    def exists(self, relative_path: str) -> bool:
        """返回逻辑路径处的对象是否存在。"""
        key = self._key(relative_path)
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise

    def write_artifact_file(
        self,
        document_id: str,
        *,
        version: int = 1,
        relative_name: str,
        data: bytes,
    ) -> str:
        """向文档 artifact 前缀写入附加对象，返回逻辑相对路径。"""
        safe = relative_name.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError("invalid artifact relative path")
        prefix = self._key(str(self.artifact_dir(document_id, version)))
        key = f"{prefix}/{safe}"
        self._put_bytes(key, data)
        # 与本地 BlobStore 对齐：返回相对 storage 根的路径形态
        return f"artifacts/{document_id}/v{version}/{safe}"

    def list_artifacts(self, document_id: str, version: int = 1) -> list[str]:
        """列出文档版本前缀下的产物相对路径。"""
        prefix = self._key(str(self.artifact_dir(document_id, version))).rstrip("/") + "/"
        names: list[str] = []
        for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
            name = obj.object_name[len(prefix) :]
            if name:
                names.append(name)
        return sorted(names)

    def artifact_path(self, blob_path: str, filename: str) -> str:
        """将已保存的对象路径和产物文件名拼成对象存储键。"""
        return self._key(f"{blob_path.rstrip('/')}/{filename}")

    def _put_bytes(self, key: str, data: bytes) -> None:
        """确保存储桶存在，并将字节上传到指定对象存储键。"""
        ensure_bucket()
        self._client.put_object(
            self._bucket,
            key,
            io.BytesIO(data),
            length=len(data),
        )

    def _put_text(self, key: str, text: str) -> None:
        """将文本编码为 UTF-8 并上传到指定对象存储键。"""
        self._put_bytes(key, text.encode("utf-8"))
