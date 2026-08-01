"""TrustGuard 网络安全语料采集子系统。"""

from app.core.crawler.cleaning import CleaningOutcome, CrawlerCleaner
from app.core.crawler.engine import CrawlEngine, CrawlPage, CrawlRequest
from app.core.crawler.presets import (
    CRAWLER_PRESETS,
    CrawlerPreset,
    expand_crawler_presets,
)
from app.core.crawler.runner import CrawlerRunner, get_crawler_runner
from app.core.crawler.structured import (
    StructuredSourceRegistry,
    default_structured_registry,
)

__all__ = [
    "CleaningOutcome",
    "CrawlEngine",
    "CrawlPage",
    "CrawlRequest",
    "CRAWLER_PRESETS",
    "CrawlerPreset",
    "CrawlerRunner",
    "CrawlerCleaner",
    "StructuredSourceRegistry",
    "default_structured_registry",
    "expand_crawler_presets",
    "get_crawler_runner",
]
