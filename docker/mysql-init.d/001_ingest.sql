-- trustguard-rag-platform 的单租户入库表结构

CREATE TABLE IF NOT EXISTS documents (
    id CHAR(36) NOT NULL PRIMARY KEY,
    source_type VARCHAR(32) NOT NULL,
    source_uri VARCHAR(2048) NOT NULL,
    content_hash CHAR(64) NOT NULL,
    title VARCHAR(512) NULL,
    mime_type VARCHAR(128) NULL,
    original_filename VARCHAR(512) NULL,
    doc_version INT NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL,
    blob_path VARCHAR(512) NULL,
    metadata_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_document_source (source_type, source_uri(256), content_hash),
    KEY idx_documents_filename (original_filename),
    KEY idx_documents_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chunks (
    id CHAR(36) NOT NULL PRIMARY KEY,
    document_id CHAR(36) NOT NULL,
    chunk_index INT NOT NULL,
    text MEDIUMTEXT NOT NULL,
    token_count INT NOT NULL DEFAULT 0,
    page_no INT NULL,
    embedding_model VARCHAR(64) NULL,
    embedding_dim INT NULL,
    qdrant_point_id VARCHAR(64) NULL,
    metadata_json JSON NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    KEY idx_chunks_document (document_id),
    CONSTRAINT fk_chunks_document FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id CHAR(36) NOT NULL PRIMARY KEY,
    source_type VARCHAR(32) NOT NULL,
    source TEXT NOT NULL,
    options_json JSON NULL,
    status VARCHAR(32) NOT NULL,
    current_step VARCHAR(32) NULL,
    document_id CHAR(36) NULL,
    pending_document_id CHAR(36) NULL,
    conflict_candidates_json JSON NULL,
    attempt INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    error_code VARCHAR(64) NULL,
    error_message TEXT NULL,
    step_logs JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    started_at DATETIME(6) NULL,
    lease_owner VARCHAR(128) NULL,
    lease_token CHAR(36) NULL,
    lease_expires_at DATETIME(6) NULL,
    heartbeat_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    KEY idx_jobs_status (status),
    KEY idx_jobs_document (document_id),
    KEY idx_jobs_lease (status, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ingest_cursors (
    cursor_key VARCHAR(64) NOT NULL PRIMARY KEY,
    cursor_value VARCHAR(256) NOT NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
