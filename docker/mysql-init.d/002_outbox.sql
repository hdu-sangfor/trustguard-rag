-- RabbitMQ 事务性 Outbox。仅在 MySQL 初始化新数据卷时自动执行此脚本；
-- 对现有数据库需要手动执行。

CREATE TABLE IF NOT EXISTS outbox_events (
    id CHAR(36) NOT NULL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    payload_json JSON NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempt INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 20,
    next_attempt_at DATETIME(6) NULL,
    locked_at DATETIME(6) NULL,
    last_error TEXT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    published_at DATETIME(6) NULL,
    KEY idx_outbox_dispatch (status, next_attempt_at, created_at),
    KEY idx_outbox_aggregate (aggregate_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
