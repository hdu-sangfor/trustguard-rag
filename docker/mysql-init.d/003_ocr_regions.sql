CREATE TABLE IF NOT EXISTS ocr_regions (
  id VARCHAR(36) PRIMARY KEY,
  document_id VARCHAR(36) NOT NULL,
  page_no INT NULL,
  bbox_json JSON NULL,
  crop_blob_path VARCHAR(512) NULL,
  ocr_text MEDIUMTEXT NOT NULL,
  corrected_text MEDIUMTEXT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  provider VARCHAR(64) NULL,
  confidence DOUBLE NULL,
  error_message TEXT NULL,
  metadata_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_ocr_document (document_id, status)
);
