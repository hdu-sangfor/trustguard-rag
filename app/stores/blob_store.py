"""带暂存和提交语义的本地文件系统 blob 存储。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.settings import get_settings


class BlobStore:
    def __init__(self, root: Path | None = None) -> None:
        """初始化本地 blob 根目录和暂存目录。"""
        s = get_settings()
        self._root = (root or Path(s.local_storage_dir)).resolve()
        self._staging = Path(s.staging_dir).resolve()

    def _resolved_under_root(self, relative_path: str) -> Path:
        """将相对路径解析到 blob 根下，拒绝越界。"""
        rel = relative_path.replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise ValueError(f"invalid blob path: {relative_path}")
        target = (self._root / rel).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as e:
            raise ValueError(f"blob path escapes storage root: {relative_path}") from e
        return target

    @property
    def root(self) -> Path:
        """返回已提交 artifacts 的本地文件系统根目录。"""
        return self._root

    def artifact_dir(self, document_id: str, version: int = 1) -> Path:
        """返回某个文档版本的已提交 artifact 目录。"""
        return self._root / "artifacts" / document_id / f"v{version}"

    def job_upload_path(self, job_id: str) -> Path:
        """返回保存任务原始上传文件的暂存路径。"""
        return self._staging / "jobs" / job_id / "upload"

    def put_job_upload(self, job_id: str, data: bytes) -> Path:
        """将上传文件保存到任务暂存区并返回路径。"""
        path = self.job_upload_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_job_upload(self, job_id: str) -> bytes:
        """从任务暂存区读取上传文件字节。"""
        return self.job_upload_path(job_id).read_bytes()

    def put_staging(self, staging_key: str, filename: str, data: bytes) -> Path:
        """在配置的暂存区下写入任意暂存文件。"""
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
        """为文档原子提交原始文件、抽取文本和元数据 artifacts。"""
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
        """删除相对前缀标识的已提交文件或目录。"""
        target = self._root / prefix if not Path(prefix).is_absolute() else Path(prefix)
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        if target.exists():
            raise RuntimeError(f"artifact path still exists after deletion: {target}")

    def delete_staging(self, staging_key: str) -> None:
        """在任务成功、失败或丢弃后删除对应暂存子树。"""
        path = self._staging / staging_key
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def delete_job_staging(self, job_id: str) -> None:
        """删除单个入库任务的所有暂存文件。"""
        self.delete_staging(f"jobs/{job_id}")

    def read(self, relative_path: str) -> bytes:
        """按相对 blob 根目录的路径读取已提交 artifact 字节。"""
        return self._resolved_under_root(relative_path).read_bytes()

    def read_text(self, relative_path: str) -> str:
        """读取已提交的 UTF-8 文本 artifact。"""
        return self._resolved_under_root(relative_path).read_text(encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        """返回已提交 artifact 路径是否存在。"""
        try:
            return self._resolved_under_root(relative_path).exists()
        except ValueError:
            return False

    def list_artifacts(self, document_id: str, version: int = 1) -> list[str]:
        """列出已提交文档 artifact 包中的文件名（含子目录相对路径）。"""
        bundle = self.artifact_dir(document_id, version)
        if not bundle.exists():
            return []
        names: list[str] = []
        for p in bundle.rglob("*"):
            if p.is_file():
                names.append(str(p.relative_to(bundle)).replace("\\", "/"))
        return names

    def write_artifact_file(
        self,
        document_id: str,
        *,
        version: int = 1,
        relative_name: str,
        data: bytes,
    ) -> str:
        """向已提交 artifact 目录写入附加文件（如 OCR crop），返回相对根目录路径。"""
        safe = relative_name.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError("invalid artifact relative path")
        path = self.artifact_dir(document_id, version) / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(self._root)).replace("\\", "/")


def get_blob_store() -> BlobStore:
    """选择当前配置的 blob 存储后端。"""
    s = get_settings()
    if s.minio_enabled:
        from app.stores.minio_blob_store import MinioBlobStore

        return MinioBlobStore()  # type: ignore[return-value]
    return BlobStore()
