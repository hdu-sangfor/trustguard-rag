CREATE TABLE IF NOT EXISTS knowledge_scopes (
    scope VARCHAR(64) NOT NULL PRIMARY KEY,
    default_mode VARCHAR(32) NOT NULL DEFAULT 'auto',
    per_knowledge_base_limit INT NOT NULL DEFAULT 20,
    allowed_content_types JSON NOT NULL,
    allowed_workflow_types JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_scope_bindings (
    scope VARCHAR(64) NOT NULL,
    knowledge_base_id CHAR(36) NOT NULL,
    position INT NOT NULL DEFAULT 0,
    binding_type VARCHAR(32) NOT NULL DEFAULT 'manual',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (scope, knowledge_base_id),
    KEY idx_knowledge_scope_binding_kb (knowledge_base_id),
    CONSTRAINT fk_knowledge_scope_binding_scope
        FOREIGN KEY (scope) REFERENCES knowledge_scopes(scope) ON DELETE CASCADE,
    CONSTRAINT fk_knowledge_scope_binding_kb
        FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
