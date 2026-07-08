"""Shared MinIO client and bucket bootstrap."""
from __future__ import annotations

from functools import lru_cache

from minio import Minio

from app.settings import get_settings


@lru_cache
def get_minio_client() -> Minio:
    s = get_settings()
    return Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=s.minio_secure,
    )


def ensure_bucket() -> None:
    s = get_settings()
    client = get_minio_client()
    if not client.bucket_exists(s.minio_bucket):
        client.make_bucket(s.minio_bucket)


def clear_minio_client_cache() -> None:
    get_minio_client.cache_clear()
