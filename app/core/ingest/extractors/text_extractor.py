"""纯文本 / Markdown / CSV / JSON 抽取。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from typing import Any

from app.core.ingest.errors import CORRUPT_FILE, EMPTY_CONTENT, FILE_TOO_LARGE, IngestError
from app.core.ingest.models import ExtractedDocument
from app.settings import get_settings

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestError(CORRUPT_FILE, "Unable to decode text file")


def _check_size(data: bytes) -> None:
    settings = get_settings()
    if len(data) > settings.ingest_max_file_bytes:
        raise IngestError(FILE_TOO_LARGE, "File exceeds max size")


class PlainTextExtractor:
    """text/plain 抽取。"""

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.txt",
        mime: str = "text/plain",
        raw_filename: str = "raw.txt",
    ) -> ExtractedDocument:
        _check_size(data)
        text = _decode_text(data).strip()
        if not text:
            raise IngestError(EMPTY_CONTENT, "Text file is empty")
        content_hash = hashlib.sha256(data).hexdigest()
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime=mime,
            raw_bytes=data,
            raw_filename=raw_filename,
            metadata={
                "original_filename": original_filename,
                "file_size": len(data),
            },
        )


class MarkdownExtractor(PlainTextExtractor):
    """Markdown：保留正文，剥离 YAML front matter 到 metadata。"""

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.md",
        mime: str = "text/markdown",
        raw_filename: str = "raw.md",
    ) -> ExtractedDocument:
        _check_size(data)
        raw = _decode_text(data)
        front: dict[str, Any] = {}
        body = raw
        match = _FRONT_MATTER.match(raw)
        if match:
            # 不解析完整 YAML，仅保留原文块
            front["front_matter"] = match.group(1).strip()
            body = raw[match.end() :]
        text = body.strip()
        if not text:
            raise IngestError(EMPTY_CONTENT, "Markdown file is empty")
        content_hash = hashlib.sha256(data).hexdigest()
        return ExtractedDocument(
            text=text,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime=mime,
            raw_bytes=data,
            raw_filename=raw_filename,
            metadata={
                "original_filename": original_filename,
                "file_size": len(data),
                **front,
            },
        )


class CsvExtractor:
    """CSV 转为可读文本行。"""

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.csv",
    ) -> ExtractedDocument:
        _check_size(data)
        text = _decode_text(data)
        reader = csv.reader(io.StringIO(text))
        lines: list[str] = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            lines.append(" | ".join(cell.strip() for cell in row))
        joined = "\n".join(lines).strip()
        if not joined:
            raise IngestError(EMPTY_CONTENT, "CSV file is empty")
        content_hash = hashlib.sha256(data).hexdigest()
        return ExtractedDocument(
            text=joined,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime="text/csv",
            raw_bytes=data,
            raw_filename="raw.csv",
            metadata={
                "original_filename": original_filename,
                "file_size": len(data),
                "row_count": len(lines),
            },
        )


class JsonExtractor:
    """JSON 转为 pretty 文本（超长截断）。"""

    def extract(
        self,
        data: bytes,
        *,
        original_filename: str = "document.json",
    ) -> ExtractedDocument:
        _check_size(data)
        try:
            obj = json.loads(_decode_text(data))
        except json.JSONDecodeError as e:
            raise IngestError(CORRUPT_FILE, f"Invalid JSON: {e}") from e
        pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        max_chars = get_settings().ingest_json_max_chars
        truncated = False
        if len(pretty) > max_chars:
            pretty = pretty[:max_chars] + "\n...[truncated]"
            truncated = True
        if not pretty.strip():
            raise IngestError(EMPTY_CONTENT, "JSON content is empty")
        content_hash = hashlib.sha256(data).hexdigest()
        return ExtractedDocument(
            text=pretty,
            content_hash=content_hash,
            source_uri=f"upload://{content_hash}",
            mime="application/json",
            raw_bytes=data,
            raw_filename="raw.json",
            metadata={
                "original_filename": original_filename,
                "file_size": len(data),
                "truncated": truncated,
            },
        )
