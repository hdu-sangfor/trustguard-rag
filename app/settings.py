"""应用配置：所有运行参数来自环境变量（前缀 RAG_），便于独立部署与容器化。

见 doc/rag-platform-implementation-plan.md §5（基础设施选型）。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import model_validator
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
    app_env: str = "dev"  # 运行环境取值：dev | prod
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 18200
    rag_mode: str = "ingest"  # 健康检查模式取值：ingest | full

    # --- 入库 ---
    ingest_max_pdf_bytes: int = 52_428_800
    ingest_max_file_bytes: int = 52_428_800
    ingest_max_pdf_pages: int = 500
    conflict_ttl_hours: int = 168
    chunk_tokenizer_model: str = "Qwen/Qwen3-Embedding-0.6B"
    chunk_target_tokens: int = 384
    chunk_overlap_tokens: int = 64
    ingest_json_max_chars: int = 200_000

    # --- OCR ---
    ocr_provider: str = "none"  # none | local | api
    ocr_api_driver: str = "openai_compatible"  # bailian | openai_compatible | custom
    ocr_lang: str = "ch"
    ocr_fail_open: bool = True
    ocr_render_dpi: int = 144
    ocr_min_image_side_px: int = 32
    ocr_max_regions_per_page: int = 32
    ocr_max_regions_per_document: int = 200
    ocr_max_crop_pixels: int = 4_000_000
    ocr_max_crop_bytes: int = 8_000_000
    ocr_api_base_url: str | None = None
    ocr_api_key: str | None = None
    ocr_api_model: str = "qwen-vl-ocr"
    ocr_api_timeout_seconds: float = 60.0
    ocr_api_prompt: str | None = None
    ocr_custom_base_url: str | None = None
    ocr_custom_path: str = "/ocr"
    ocr_custom_api_key: str | None = None
    ocr_custom_headers_json: str | None = None
    ocr_custom_request_template: str = "multipart"  # multipart | base64_json
    ocr_custom_response_jsonpath: str = "$.text"
    # 默认拒绝 custom/API OCR 指向内网/回环；本地联调可设 true
    ocr_allow_private_urls: bool = False

    # --- 文档解析器 / MinerU ---
    pdf_parser: str = "mineru"  # mineru | local（显式回退）
    docx_parser: str = "local"  # local | mineru
    mineru_base_url: str = "http://127.0.0.1:8000"
    mineru_backend: str = "pipeline"
    mineru_timeout_seconds: float = 300.0

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
    qdrant_mock: bool = True  # 设为 True 时跳过真实 Qdrant，索引操作为空操作

    # --- OpenSearch（BM25 / 全文） ---
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_user: str | None = None
    opensearch_password: str | None = None
    opensearch_use_ssl: bool = False
    opensearch_verify_certs: bool = False
    opensearch_index_prefix: str = "rag_"
    opensearch_backfill_on_startup: bool = True

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
    rabbitmq_exchange: str = "rag.commands"
    rabbitmq_dead_exchange: str = "rag.dead"
    rabbitmq_prefetch_count: int = 1
    rabbitmq_consumer_max_retries: int = 5
    rabbitmq_retry_delays_ms: str = "10000,60000,300000"
    worker_outbox_poll_seconds: float = 1.0
    worker_outbox_batch_size: int = 50
    worker_outbox_lease_seconds: int = 60
    worker_job_lease_seconds: int = 120
    worker_heartbeat_seconds: float = 30.0
    worker_recovery_scan_seconds: float = 15.0
    worker_indexing_stale_seconds: int = 300
    worker_eager: bool = False

    # --- 对象存储（MVP 可选：默认本地文件后端，见 §3 / §5.1） ---
    minio_enabled: bool = False
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "trustguard-rag-artifacts"
    local_storage_dir: str = "./data/storage"  # `minio_enabled` 为 False 时使用

    # --- 嵌入（启动前需冻结单一模型，维度必须与模型匹配；见 §5.1“嵌入模型冻结”） ---
    embedding_provider: str = "pseudo"  # 提供方取值：pseudo | local | api | openai_compatible
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_dim: int = 1024
    embedding_device: str = "auto"
    embedding_batch_size: int = 10
    embedding_normalize: bool = True
    embedding_query_instruction: str = (
        "Given a cybersecurity search query, retrieve relevant passages that answer the query"
    )
    embedding_download_source: str = "huggingface"  # 下载源取值：huggingface | modelscope
    embedding_cache_dir: str | None = None
    huggingface_endpoint: str | None = None
    huggingface_hub_url: str | None = None
    modelscope_endpoint: str | None = None
    modelscope_cache_dir: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_api_timeout_seconds: float = 60.0

    # --- 重排序 ---
    rerank_provider: str = "none"  # 重排序提供方取值：none | local | api
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_top_k: int = 10  # 重排序前传入的候选数量
    rerank_device: str = "auto"
    rerank_batch_size: int = 16
    rerank_normalize: bool = True
    rerank_query_max_length: int = 512
    rerank_passage_max_length: int = 8192
    rerank_base_url: str | None = None
    rerank_api_key: str | None = None
    rerank_api_timeout_seconds: float = 60.0
    rerank_instruction: str | None = None

    # --- 混合检索 ---
    search_top_k: int = 10  # 最终返回的结果数量
    search_vector_top_k: int = 30  # 向量检索引擎初始召回数量
    search_keyword_top_k: int = 30  # 关键词检索引擎初始召回数量
    search_fusion_method: str = "rrf"  # 融合策略取值：rrf | weighted_score
    search_rrf_k: int = 60  # RRF 融合常数，越小则排名靠前的权重越高
    search_vector_weight: float = 0.6  # 加权分数模式下的向量检索权重
    search_keyword_weight: float = 0.4  # 加权分数模式下的关键词检索权重
    search_opensearch_mock: bool = True  # 设为 True 时使用内存模拟 BM25，无需真实 OpenSearch

    # --- 健康检查 ---
    health_check_timeout_seconds: float = 3.0

    @model_validator(mode="after")
    def reject_mock_retrieval_in_production(self) -> Settings:
        """校验生产检索后端和分块窗口配置。"""
        if self.pdf_parser.strip().lower() not in {"local", "mineru"}:
            raise ValueError("RAG_PDF_PARSER 必须是 local 或 mineru")
        if self.docx_parser.strip().lower() not in {"local", "mineru"}:
            raise ValueError("RAG_DOCX_PARSER 必须是 local 或 mineru")
        if self.chunk_target_tokens <= 0:
            raise ValueError("RAG_CHUNK_TARGET_TOKENS 必须大于 0")
        if not 0 <= self.chunk_overlap_tokens < self.chunk_target_tokens:
            raise ValueError(
                "RAG_CHUNK_OVERLAP_TOKENS 必须大于等于 0，并且小于 RAG_CHUNK_TARGET_TOKENS"
            )
        if self.app_env.strip().lower() != "prod":
            return self
        enabled_mocks: list[str] = []
        if self.qdrant_mock:
            enabled_mocks.append("RAG_QDRANT_MOCK")
        if self.search_opensearch_mock:
            enabled_mocks.append("RAG_SEARCH_OPENSEARCH_MOCK")
        if enabled_mocks:
            raise ValueError(
                "Production cannot enable mock retrieval backends: " + ", ".join(enabled_mocks)
            )
        return self

    @property
    def staging_dir(self) -> str:
        """返回产物提交前使用的本地暂存目录。"""
        return f"{self.local_storage_dir.rstrip('/')}/staging"

    @property
    def mysql_dsn(self) -> str:
        """根据配置构建 SQLAlchemy 异步 MySQL 连接串。"""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def qdrant_url(self) -> str:
        """构建 Qdrant 客户端使用的基础 URL。"""
        return f"http://{self.qdrant_host}:{self.qdrant_port}"

    @property
    def redis_url(self) -> str:
        """构建 Redis URL，包含配置中的密码和数据库编号。"""
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def rabbitmq_url(self) -> str:
        """构建 aio-pika 使用的 AMQP URL。"""
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}{self.rabbitmq_vhost}"
        )

    @property
    def rabbitmq_retry_delays(self) -> tuple[int, ...]:
        """解析消费者重试队列使用的毫秒级退避时间。"""
        values = tuple(
            int(value.strip())
            for value in self.rabbitmq_retry_delays_ms.split(",")
            if value.strip()
        )
        return values or (30_000,)


@lru_cache
def get_settings() -> Settings:
    """返回从环境变量加载并缓存的应用配置。"""
    return Settings()
