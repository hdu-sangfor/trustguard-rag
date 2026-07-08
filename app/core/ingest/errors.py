"""Ingest error codes and exceptions."""
from __future__ import annotations


class IngestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


CORRUPT_FILE = "CORRUPT_FILE"
PDF_ENCRYPTED = "PDF_ENCRYPTED"
PDF_TOO_LARGE = "PDF_TOO_LARGE"
PDF_NO_TEXT_LAYER = "PDF_NO_TEXT_LAYER"
EMPTY_CONTENT = "EMPTY_CONTENT"
FILENAME_CONFLICT = "FILENAME_CONFLICT"
SOURCE_CONFLICT = "SOURCE_CONFLICT"
ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
INDEX_FAILED = "INDEX_FAILED"
UNSUPPORTED_MIME = "UNSUPPORTED_MIME"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
