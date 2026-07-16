"""共享 MinIO 客户端和存储桶初始化。"""
from __future__ import annotations

from functools import lru_cache

from minio import Minio

from app.settings import get_settings


@lru_cache
def get_minio_client() -> Minio:
    """返回根据应用配置创建并缓存的 MinIO 客户端。"""
    s = get_settings()
    return Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
    )


def ensure_bucket() -> None:
    """在配置的存储桶不存在时创建它。"""
    s = get_settings()
    client = get_minio_client()
    if not client.bucket_exists(s.minio_bucket):
        client.make_bucket(s.minio_bucket)


def clear_minio_client_cache() -> None:
    """清空客户端缓存，便于测试或配置变更后重建。"""
    get_minio_client.cache_clear()
