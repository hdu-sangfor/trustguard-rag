"""Crawler 任务领域值。"""

from __future__ import annotations

from enum import StrEnum


class CrawlJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


CRAWL_TERMINAL_STATUSES = frozenset(
    {
        CrawlJobStatus.SUCCEEDED,
        CrawlJobStatus.CANCELLED,
    }
)
