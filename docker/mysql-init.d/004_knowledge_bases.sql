CREATE TABLE IF NOT EXISTS knowledge_bases (
    id CHAR(36) NOT NULL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    description VARCHAR(1024) NULL,
    embedding_profile VARCHAR(64) NOT NULL,
    embedding_provider VARCHAR(32) NOT NULL,
    embedding_api_driver VARCHAR(32) NOT NULL DEFAULT 'openai_compatible',
    embedding_model VARCHAR(128) NOT NULL,
    embedding_dim INT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uq_knowledge_base_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SET @column_exists = (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'documents'
      AND COLUMN_NAME = 'knowledge_base_id'
);
SET @add_column = IF(
    @column_exists = 0,
    'ALTER TABLE documents ADD COLUMN knowledge_base_id CHAR(36) NULL, ADD KEY idx_documents_knowledge_base (knowledge_base_id)',
    'SELECT 1'
);
PREPARE statement FROM @add_column;
EXECUTE statement;
DEALLOCATE PREPARE statement;

SET @old_unique_exists = (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'documents'
      AND INDEX_NAME = 'uq_document_source'
);
SET @replace_unique = IF(
    @old_unique_exists > 0,
    'ALTER TABLE documents DROP INDEX uq_document_source, ADD UNIQUE KEY uq_document_kb_source (knowledge_base_id, source_type, source_uri(256), content_hash)',
    'SELECT 1'
);
PREPARE statement FROM @replace_unique;
EXECUTE statement;
DEALLOCATE PREPARE statement;
