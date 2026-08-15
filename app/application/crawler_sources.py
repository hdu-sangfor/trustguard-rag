"""Application service for persisted crawler sources and scheduled jobs."""

from __future__ import annotations

from typing import Any

from app.stores.crawler_source_store import CrawlerSourceStore
from app.stores.crawler_store import CrawlerStore
from app.workers.eager import dispatch_eager


_LIST_KEYS = (
    "preset_ids",
    "urls",
    "keywords",
    "site_urls",
    "rss_urls",
    "structured_sources",
)


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def config_from_source(row, *, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(row.config_json or {})
    for key in _LIST_KEYS:
        config[key] = _unique(list(config.get(key) or []))
    config["source_options"] = dict(config.get("source_options") or {})
    endpoint = str(row.endpoint or "").strip()
    if row.source_kind == "url" and endpoint:
        config["urls"] = _unique([*config["urls"], endpoint])
    elif row.source_kind == "site" and endpoint:
        config["site_urls"] = _unique([*config["site_urls"], endpoint])
    elif row.source_kind == "rss" and endpoint:
        config["rss_urls"] = _unique([*config["rss_urls"], endpoint])
    elif row.source_kind == "structured" and endpoint:
        config["structured_sources"] = _unique(
            [*config["structured_sources"], endpoint]
        )
    config.update(
        {
            "source_id": row.id,
            "source_ids": [row.id],
            "incremental": True,
            "source_trust_level": row.trust_level,
            "source_content_type": row.content_type,
            "source_usage_restrictions": row.usage_restrictions,
            "force": False,
            "require_review": bool(config.get("require_review", True)),
            "review_mode": str(config.get("review_mode") or "human"),
            "review_criteria": str(config.get("review_criteria") or ""),
        }
    )
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                config[key] = value
    return config


async def trigger_crawler_source(
    source_id: str,
    *,
    trigger_type: str = "manual",
    overrides: dict[str, Any] | None = None,
):
    sources = CrawlerSourceStore()
    source = await sources.get(source_id)
    if source is None:
        raise LookupError("Crawler source not found")
    if not source.enabled:
        raise ValueError("Crawler source is disabled")
    config = config_from_source(source, overrides=overrides)
    crawler = CrawlerStore()
    row, event = await crawler.create_job(
        knowledge_base_id=source.knowledge_base_id,
        config=config,
    )
    try:
        await sources.register_run(
            source_id=source.id,
            crawl_job_id=row.id,
            trigger_type=trigger_type,
        )
    except Exception:
        # The crawl command remains recoverable through the outbox. Keeping the
        # source-run registration transactional is handled by a future schema merge.
        raise
    await dispatch_eager(event)
    return await crawler.get(row.id) or row


async def merge_registered_source_config(config: dict[str, Any]) -> dict[str, Any]:
    """Apply database-managed source definitions after built-in preset expansion."""
    source_ids = _unique(list(config.get("source_ids") or []))
    sources = CrawlerSourceStore()
    selected = []
    for source_id in source_ids:
        row = await sources.get(source_id)
        if row is None:
            raise LookupError(f"Crawler source not found: {source_id}")
        if not row.enabled:
            raise ValueError(f"Crawler source is disabled: {source_id}")
        selected.append(row)
    if not selected and config.get("preset_ids"):
        preset_rows = await sources.configs_for_presets(list(config["preset_ids"]))
        managed = [row for row in preset_rows if row.source_kind == "preset"]
        selected = managed or preset_rows
    if not selected:
        return config
    # One source identity keeps HTTP validators, run statistics and version history
    # unambiguous. A preset source may still fan out to many endpoints internally.
    if len(selected) > 1:
        raise ValueError("A crawler job can use at most one managed source")
    managed = config_from_source(selected[0])
    for key in _LIST_KEYS:
        explicit = list(config.get(key) or [])
        config[key] = _unique([*managed.get(key, []), *explicit])
    merged_options = dict(managed.get("source_options") or {})
    for source_name, options in dict(config.get("source_options") or {}).items():
        merged_options[source_name] = {
            **dict(merged_options.get(source_name) or {}),
            **dict(options or {}),
        }
    config["source_options"] = merged_options
    for key, value in managed.items():
        if key not in _LIST_KEYS and key != "source_options":
            if key in {
                "force",
                "review_mode",
                "review_criteria",
                "require_review",
            } and key in config:
                continue
            config[key] = value
    return config
