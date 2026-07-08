-- M0/M1 初始化 schema，见 doc/rag-platform-implementation-plan.md §8.1。
-- 通过 docker-compose 挂载到 mysql 的 /docker-entrypoint-initdb.d 自动执行（仅首次建库时）。

CREATE TABLE IF NOT EXISTS rag_documents (
  id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(128),
  project_id VARCHAR(128),
  source_type VARCHAR(64) NOT NULL,
  source_url TEXT,
  title TEXT,
  content_hash VARCHAR(128),
  raw_object_key TEXT,
  clean_object_key TEXT,
  source_trust FLOAT DEFAULT 0.5,
  status VARCHAR(32) NOT NULL,
  fetched_at DATETIME,
  parsed_at DATETIME,
  indexed_at DATETIME,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  KEY idx_documents_tenant_project (tenant_id, project_id),
  KEY idx_documents_source_type (source_type),
  KEY idx_documents_content_hash (content_hash),
  KEY idx_documents_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rag_chunks (
  id VARCHAR(64) PRIMARY KEY,
  document_id VARCHAR(64) NOT NULL,
  parent_chunk_id VARCHAR(64),
  chunk_type VARCHAR(64) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  token_count INT,
  entities_json JSON,
  metadata_json JSON,
  source_start INT,
  source_end INT,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  KEY idx_chunks_document (document_id),
  KEY idx_chunks_parent (parent_chunk_id),
  KEY idx_chunks_type (chunk_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rag_jobs (
  id VARCHAR(64) PRIMARY KEY,
  job_type VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL,
  current_step VARCHAR(64),
  payload_json JSON,
  error TEXT,
  retry_count INT DEFAULT 0,
  created_at DATETIME NOT NULL,
  started_at DATETIME,
  finished_at DATETIME,
  KEY idx_jobs_status (status),
  KEY idx_jobs_type (job_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
