"""Source-aware content cleaning between crawling and RAG ingestion."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from app.core.crawler.engine import CrawlPage

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|$)", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\([^)]*\)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$", re.MULTILINE)
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_MARKDOWN_SYNTAX = re.compile(r"[#>*_`|~\[\](){}\-=:]+")
_SOURCE_LINE = re.compile(r"^\s*>\s*(?:来源|source)\s*[：:].*$", re.MULTILINE | re.IGNORECASE)

_REJECTED_STATUSES = frozenset({"rejected", "reserved"})


@dataclass(frozen=True, slots=True)
class CleaningOutcome:
    page: CrawlPage | None
    rejected_reason: str | None = None
    changes: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        return self.page is None


class CrawlerCleaner:
    """Apply deterministic generic and source-specific cleaning rules."""

    version = "crawler-cleaner-v1"

    def clean(self, page: CrawlPage, *, min_content_chars: int = 80) -> CleaningOutcome:
        reason = self._rejection_reason(page)
        if reason:
            return CleaningOutcome(page=None, rejected_reason=reason)

        text = page.markdown
        changes: list[str] = []
        text = self._apply(text, _CONTROL_CHARACTERS.sub("", text), "control_characters", changes)
        text = self._apply(text, _FRONT_MATTER.sub("", text), "front_matter", changes)
        text = self._apply(text, re.sub(r"\bhxxps://", "https://", text), "defanged_https", changes)
        text = self._apply(text, re.sub(r"\bhxxp://", "http://", text), "defanged_http", changes)
        text = self._apply(text, text.replace("[.]", "."), "defanged_dots", changes)
        text = self._apply(text, _MARKDOWN_IMAGE.sub("", text), "markdown_images", changes)

        if self._is_owasp(page):
            text = self._apply(text, _HTML_TAG.sub("", text), "html_tags", changes)
            text = self._apply(text, _MARKDOWN_LINK.sub(r"\1", text), "markdown_links", changes)
            text = self._apply(text, _TABLE_SEPARATOR.sub("", text), "table_separators", changes)

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = _TRAILING_SPACE.sub("", normalized)
        normalized = _EXCESS_BLANK_LINES.sub("\n\n", normalized).strip()
        text = self._apply(text, normalized, "whitespace", changes)

        meaningful = _SOURCE_LINE.sub("", text)
        meaningful = _MARKDOWN_SYNTAX.sub("", meaningful)
        meaningful_chars = len("".join(meaningful.split()))
        minimum = max(int(min_content_chars), 0)
        if meaningful_chars < minimum:
            return CleaningOutcome(
                page=None,
                rejected_reason=(
                    f"Content has {meaningful_chars} meaningful characters; minimum is {minimum}"
                ),
                changes=tuple(changes),
            )

        metadata = {
            **page.metadata,
            "cleaning_version": self.version,
            "cleaning_changes": changes,
            "cleaned": True,
            "meaningful_chars": meaningful_chars,
        }
        cleaned_page = replace(
            page,
            markdown=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            metadata=metadata,
        )
        return CleaningOutcome(page=cleaned_page, changes=tuple(changes))

    @staticmethod
    def _apply(before: str, after: str, change: str, changes: list[str]) -> str:
        if before != after:
            changes.append(change)
        return after

    @staticmethod
    def _rejection_reason(page: CrawlPage) -> str | None:
        metadata = page.metadata
        status = (
            str(
                metadata.get("vulnerability_status")
                or metadata.get("record_status")
                or metadata.get("status")
                or ""
            )
            .strip()
            .lower()
        )
        if status in _REJECTED_STATUSES:
            return f"Source record status is {status.upper()}"
        if metadata.get("deprecated") is True:
            return "Source record is deprecated"
        if metadata.get("revoked") is True:
            return "Source record is revoked"
        return None

    @staticmethod
    def _is_owasp(page: CrawlPage) -> bool:
        adapter = str(page.metadata.get("source_adapter") or "").lower()
        host = (urlsplit(page.url).hostname or "").lower()
        return adapter == "owasp" or host == "owasp.org" or host.endswith(".owasp.org")
