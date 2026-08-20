CREATE TABLE IF NOT EXISTS crawler_sources (
    id VARCHAR(64) NOT NULL PRIMARY KEY,
    knowledge_base_id CHAR(36) NOT NULL,
    name VARCHAR(128) NOT NULL,
    description TEXT NULL,
    source_kind VARCHAR(32) NOT NULL,
    endpoint VARCHAR(2048) NULL,
    preset_ids_json JSON NOT NULL,
    config_json JSON NOT NULL,
    trust_level VARCHAR(32) NOT NULL DEFAULT 'trusted',
    content_type VARCHAR(64) NOT NULL DEFAULT 'security_knowledge',
    usage_restrictions TEXT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    schedule_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    schedule_interval_minutes INT NULL,
    next_run_at DATETIME(6) NULL,
    last_run_at DATETIME(6) NULL,
    last_success_at DATETIME(6) NULL,
    last_job_id CHAR(36) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY idx_crawler_sources_due (enabled, schedule_enabled, next_run_at),
    KEY idx_crawler_sources_kb (knowledge_base_id),
    CONSTRAINT fk_crawler_sources_kb
        FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS crawler_source_runs (
    crawl_job_id CHAR(36) NOT NULL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    trigger_type VARCHAR(32) NOT NULL DEFAULT 'manual',
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    progress_json JSON NOT NULL,
    approved_count INT NOT NULL DEFAULT 0,
    rejected_count INT NOT NULL DEFAULT 0,
    error_message TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    finished_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY idx_crawler_source_runs_source (source_id, created_at),
    KEY idx_crawler_source_runs_status (status, updated_at),
    CONSTRAINT fk_crawler_source_runs_source
        FOREIGN KEY (source_id) REFERENCES crawler_sources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS crawler_resource_states (
    source_id VARCHAR(64) NOT NULL,
    url_hash CHAR(64) NOT NULL,
    url VARCHAR(2048) NOT NULL,
    etag VARCHAR(512) NULL,
    last_modified VARCHAR(512) NULL,
    current_content_hash CHAR(64) NULL,
    current_ingest_job_id CHAR(36) NULL,
    current_document_id CHAR(36) NULL,
    current_version INT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    first_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_seen_at DATETIME(6) NULL,
    last_changed_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (source_id, url_hash),
    KEY idx_crawler_resource_status (source_id, status, last_seen_at),
    CONSTRAINT fk_crawler_resource_source
        FOREIGN KEY (source_id) REFERENCES crawler_sources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS crawler_source_versions (
    id CHAR(36) NOT NULL PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    url_hash CHAR(64) NOT NULL,
    resource_url VARCHAR(2048) NOT NULL,
    crawl_job_id CHAR(36) NOT NULL,
    ingest_job_id CHAR(36) NULL,
    document_id CHAR(36) NULL,
    content_hash CHAR(64) NOT NULL,
    version INT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    supersedes_version_id CHAR(36) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    activated_at DATETIME(6) NULL,
    superseded_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_crawler_version_content (source_id, url_hash, content_hash),
    KEY idx_crawler_versions_resource (source_id, url_hash, version),
    KEY idx_crawler_versions_ingest (ingest_job_id),
    KEY idx_crawler_versions_status (status, updated_at),
    CONSTRAINT fk_crawler_version_source
        FOREIGN KEY (source_id) REFERENCES crawler_sources(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
