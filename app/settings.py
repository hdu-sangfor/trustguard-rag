"""应用配置：所有运行参数来自环境变量（前缀 RAG_），便于独立部署与容器化。

见 doc/rag-platform-implementation-plan.md §5（基础设施选型）。
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _ensure_local_no_proxy() -> None:
    """把本地地址并入 NO_PROXY，避免 httpx/qdrant-client 把对 127.0.0.1 的请求发给系统代理。"""
    existing = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for host in ("127.0.0.1", "localhost", "::1"):
        if host not in parts:
            parts.append(host)
    value = ",".join(parts)
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


_ensure_local_no_proxy()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 应用 ---
    app_name: str = "trustguard-rag-platform"
    app_version: str = "0.1.0"
    app_env: str = "dev"  # dev | prod
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 18200
    rag_mode: str = "ingest"  # ingest | full

    # --- Ingest ---
    ingest_max_pdf_bytes: int = 52_428_800
    ingest_max_pdf_pages: int = 500
    conflict_ttl_hours: int = 168
    chunk_target_tokens: int = 512

    # --- MySQL（元数据 / 文档 / 分块 / 任务） ---
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "trustguard"
    mysql_password: str = "trustguard"
    mysql_db: str = "trustguard_rag"

    # --- Qdrant（向量索引） ---
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_api_key: str | None = None
    qdrant_collection_prefix: str = "rag_"
    qdrant_mock: bool = True  # True = skip real Qdrant; index ops are no-op

    # --- OpenSearch（BM25 / 全文） ---
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_user: str | None = None
    opensearch_password: str | None = None
    opensearch_use_ssl: bool = False
    opensearch_verify_certs: bool = False
    opensearch_index_prefix: str = "rag_"

    # --- Redis（缓存 / 限流 / 任务心跳） ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None

    # --- RabbitMQ（异步任务队列 rag.*） ---
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "guest"
    rabbitmq_password: str = "guest"
    rabbitmq_vhost: str = "/"

    # --- 对象存储（MVP 可选：默认本地文件后端，见 §3 / §5.1） ---
    minio_enabled: bool = False
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "trustguard-rag-artifacts"
    local_storage_dir: str = "./data/storage"  # minio_enabled=False 时使用

    # --- Embedding（启动前需冻结单一模型，维度必须与模型匹配；见 §5.1“嵌入模型冻结”） ---
    embedding_provider: str = "openai_compatible"  # openai_compatible | local_bge
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None

    # --- Rerank ---
    rerank_provider: str = "bge"  # bge | jina | cohere | none
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # --- 健康检查 ---
    health_check_timeout_seconds: float = 3.0

    @property
    def staging_dir(self) -> str:
        return f"{self.local_storage_dir.rstrip('/')}/staging"

    @property
    def mysql_dsn(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.qdrant_host}:{self.qdrant_port}"

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}{self.rabbitmq_vhost}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
