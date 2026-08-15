"""Crawler source registry, schedules, feeds, validators, and version history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, update

from app.core.crawler.engine import CrawlEngine, CrawlPage, CrawlRequest
from app.core.crawler.runner import CrawlerRunner
from app.core.crawler.structured import StructuredSourceInfo, StructuredSourceRegistry
from app.domain import IngestJobStatus
from app.domain.crawler import CrawlJobStatus
from app.stores.crawler_source_store import (
    CrawlerSourceStore,
    seed_category_preset_sources,
)
from app.stores.crawler_store import CrawlerStore
from app.stores.job_store import JobStore
from app.stores.knowledge_base_store import KnowledgeBaseStore
from app.stores.models import CrawlJobRow, CrawlerResourceStateRow, IngestJobRow
from app.stores import db


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_rss_feed_uses_conditional_headers_and_reports_304() -> None:
    requested_headers: list[dict[str, str]] = []
    responses = [
        httpx.Response(
            200,
            headers={
                "content-type": "application/rss+xml",
                "etag": '"feed-v1"',
                "last-modified": "Fri, 14 Aug 2026 08:00:00 GMT",
            },
            text="""<?xml version="1.0"?>
            <rss version="2.0"><channel><title>Security</title><item>
              <guid>advisory-1</guid><title>Critical advisory</title>
              <link>https://example.com/advisory-1</link>
              <description>Apply the vendor patch and validate the fixed version.</description>
              <pubDate>Fri, 14 Aug 2026 08:00:00 GMT</pubDate>
            </item></channel></rss>""",
            request=httpx.Request("GET", "https://example.com/feed.xml"),
        ),
        httpx.Response(
            304,
            headers={"etag": '"feed-v1"'},
            request=httpx.Request("GET", "https://example.com/feed.xml"),
        ),
    ]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            requested_headers.append(dict(kwargs.get("headers") or {}))
            return responses.pop(0)

    async def validator(url: str, **kwargs) -> str:
        return url

    states: list[tuple[str, str | None, bool]] = []

    async def headers(url: str) -> dict[str, str]:
        return {"If-None-Match": '"feed-v1"'}

    async def state(
        url: str,
        etag: str | None,
        last_modified: str | None,
        not_modified: bool,
    ) -> None:
        states.append((url, etag, not_modified))

    engine = CrawlEngine(
        client_factory=lambda **kwargs: Client(),
        validator=validator,
    )
    request = CrawlRequest(
        rss_urls=["https://example.com/feed.xml"],
        max_total_pages=5,
        fetch_delay_seconds=0,
    )
    first = [
        page
        async for page in engine.crawl(
            request,
            conditional_headers=headers,
            on_http_state=state,
        )
    ]
    second = [
        page
        async for page in engine.crawl(
            request,
            conditional_headers=headers,
            on_http_state=state,
        )
    ]

    assert len(first) == 1
    assert first[0].source_type == "rss"
    assert first[0].metadata["feed_url"] == "https://example.com/feed.xml"
    assert second == []
    assert requested_headers == [
        {"If-None-Match": '"feed-v1"'},
        {"If-None-Match": '"feed-v1"'},
    ]
    assert states == [
        ("https://example.com/feed.xml", '"feed-v1"', False),
        ("https://example.com/feed.xml", '"feed-v1"', True),
    ]


@pytest.mark.asyncio
async def test_registry_api_manages_scheduled_rss_source(client) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    created = await client.post(
        "/v1/crawler/registry",
        json={
            "id": "cisa-alert-feed",
            "knowledge_base_id": knowledge_base.id,
            "name": "CISA Alerts",
            "source_kind": "rss",
            "endpoint": "https://www.cisa.gov/news.xml",
            "preset_ids": ["agent_08_threat_intelligence"],
            "trust_level": "official",
            "content_type": "threat_intelligence",
            "usage_restrictions": "Attribution required",
            "schedule_enabled": True,
            "schedule_interval_minutes": 60,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["schedule_enabled"] is True
    assert body["next_run_at"]
    assert body["stats"]["freshness_status"] == "never"

    updated = await client.patch(
        "/v1/crawler/registry/cisa-alert-feed",
        json={"schedule_interval_minutes": 120, "trust_level": "trusted"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["schedule_interval_minutes"] == 120

    listed = await client.get("/v1/crawler/registry?include_stats=true")
    assert listed.status_code == 200
    assert any(item["id"] == "cisa-alert-feed" for item in listed.json()["items"])

    custom = await client.post(
        "/v1/crawler/registry",
        json={
            "knowledge_base_id": knowledge_base.id,
            "name": "Form-managed recurring collection",
            "source_kind": "custom",
            "config": {"keywords": ["critical CVE"]},
            "schedule_enabled": True,
            "schedule_interval_minutes": 360,
        },
    )
    assert custom.status_code == 201, custom.text
    assert custom.json()["source_kind"] == "custom"


@pytest.mark.asyncio
async def test_run_source_reloads_job_before_serializing_response(
    client,
    monkeypatch,
) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    persisted, _ = await CrawlerStore().create_job(
        knowledge_base_id=knowledge_base.id,
        config={"source_id": "detached-response-source"},
    )

    async def detached_trigger(*_args, **_kwargs):
        return SimpleNamespace(id=persisted.id)

    monkeypatch.setattr(
        "app.api.crawler.trigger_crawler_source",
        detached_trigger,
    )

    response = await client.post(
        "/v1/crawler/registry/detached-response-source/runs",
        json={},
    )

    assert response.status_code == 202, response.text
    assert response.json()["id"] == persisted.id


@pytest.mark.asyncio
async def test_due_source_is_claimed_once(test_engine) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    store = CrawlerSourceStore()
    await store.create(
        {
            "id": "scheduled-source",
            "knowledge_base_id": knowledge_base.id,
            "name": "Scheduled source",
            "source_kind": "url",
            "endpoint": "https://example.com/security",
            "schedule_enabled": True,
            "schedule_interval_minutes": 30,
            "next_run_at": _naive_utcnow() - timedelta(minutes=1),
        }
    )

    claimed = await store.claim_due()
    claimed_again = await store.claim_due()

    assert [row.id for row in claimed] == ["scheduled-source"]
    assert claimed_again == []
    refreshed = await store.get("scheduled-source")
    assert refreshed is not None
    assert refreshed.next_run_at > _naive_utcnow()


@pytest.mark.asyncio
async def test_due_source_waits_for_active_run(test_engine) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    sources = CrawlerSourceStore()
    crawler = CrawlerStore()
    await sources.create(
        {
            "id": "non-overlapping-source",
            "knowledge_base_id": knowledge_base.id,
            "name": "Non-overlapping source",
            "source_kind": "url",
            "endpoint": "https://example.com/security",
            "schedule_enabled": True,
            "schedule_interval_minutes": 5,
            "next_run_at": _naive_utcnow() - timedelta(minutes=1),
        }
    )
    job, _ = await crawler.create_job(
        knowledge_base_id=knowledge_base.id,
        config={"source_id": "non-overlapping-source"},
    )
    await sources.register_run(
        source_id="non-overlapping-source",
        crawl_job_id=job.id,
        trigger_type="schedule",
    )

    assert await sources.claim_due() == []

    await crawler.request_cancel(job.id)
    claimed = await sources.claim_due()
    assert [row.id for row in claimed] == ["non-overlapping-source"]


@pytest.mark.asyncio
async def test_due_source_waits_for_manual_review_backlog(test_engine) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    sources = CrawlerSourceStore()
    crawler = CrawlerStore()
    await sources.create(
        {
            "id": "review-gated-source",
            "knowledge_base_id": knowledge_base.id,
            "name": "Review-gated source",
            "source_kind": "url",
            "endpoint": "https://example.com/security",
            "schedule_enabled": True,
            "schedule_interval_minutes": 5,
            "next_run_at": _naive_utcnow() - timedelta(minutes=1),
        }
    )
    job, _ = await crawler.create_job(
        knowledge_base_id=knowledge_base.id,
        config={"source_id": "review-gated-source", "review_mode": "agent"},
    )
    await sources.register_run(
        source_id="review-gated-source",
        crawl_job_id=job.id,
        trigger_type="schedule",
    )
    await crawler.finish(
        job.id,
        CrawlJobStatus.SUCCEEDED,
        progress={
            "review_mode": "agent",
            "pending_review": 1,
            "review_status": "pending",
            "review_items": [{"id": "review-1", "status": "pending"}],
        },
    )

    assert await sources.claim_due() == []

    async with db.get_engine().begin() as connection:
        await connection.execute(
            update(CrawlJobRow)
            .where(CrawlJobRow.id == job.id)
            .values(
                progress_json={
                    "review_mode": "agent",
                    "pending_review": 0,
                    "review_status": "completed",
                    "review_items": [{"id": "review-1", "status": "approved"}],
                }
            )
        )
    claimed = await sources.claim_due()
    assert [row.id for row in claimed] == ["review-gated-source"]


@pytest.mark.asyncio
async def test_stop_schedule_disables_source_and_cancels_all_active_runs(client) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    sources = CrawlerSourceStore()
    crawler = CrawlerStore()
    await sources.create(
        {
            "id": "stoppable-schedule",
            "knowledge_base_id": knowledge_base.id,
            "name": "Stoppable schedule",
            "source_kind": "url",
            "endpoint": "https://example.com/security",
            "schedule_enabled": True,
            "schedule_interval_minutes": 5,
        }
    )
    jobs = []
    for _ in range(2):
        job, _ = await crawler.create_job(
            knowledge_base_id=knowledge_base.id,
            config={"source_id": "stoppable-schedule"},
        )
        await sources.register_run(
            source_id="stoppable-schedule",
            crawl_job_id=job.id,
            trigger_type="schedule",
        )
        jobs.append(job)

    response = await client.post(
        f"/v1/crawler/jobs/{jobs[0].id}/stop?stop_schedule=true"
    )

    assert response.status_code == 200, response.text
    source = await sources.get("stoppable-schedule")
    assert source is not None
    assert source.schedule_enabled is False
    assert source.next_run_at is None
    assert [
        (await crawler.get(job.id)).status  # type: ignore[union-attr]
        for job in jobs
    ] == [CrawlJobStatus.CANCELLED, CrawlJobStatus.CANCELLED]


@pytest.mark.asyncio
async def test_source_version_reconciliation_supersedes_previous_version(test_engine) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    sources = CrawlerSourceStore()
    await sources.create(
        {
            "id": "versioned-source",
            "knowledge_base_id": knowledge_base.id,
            "name": "Versioned source",
            "source_kind": "url",
            "endpoint": "https://example.com/advisory",
        }
    )
    crawler = CrawlerStore()
    jobs = JobStore()

    async def publish_version(content_hash: str, document_id: str) -> None:
        crawl_job, _ = await crawler.create_job(
            knowledge_base_id=knowledge_base.id,
            config={"source_id": "versioned-source"},
        )
        await sources.register_run(
            source_id="versioned-source",
            crawl_job_id=crawl_job.id,
            trigger_type="manual",
        )
        ingest_job, _ = await jobs.create_ingest_command(
            job_id=str(uuid4()),
            source_type="url",
            source="https://example.com/advisory",
            knowledge_base_id=knowledge_base.id,
            options={},
        )
        await sources.record_version(
            source_id="versioned-source",
            resource_url="https://example.com/advisory",
            crawl_job_id=crawl_job.id,
            content_hash=content_hash,
            ingest_job_id=ingest_job.id,
            status="queued",
        )
        async with db.get_engine().begin() as connection:
            await connection.execute(
                update(IngestJobRow)
                .where(IngestJobRow.id == ingest_job.id)
                .values(status=IngestJobStatus.SUCCEEDED, document_id=document_id)
            )
        assert await sources.reconcile_versions() == 1

    await publish_version("a" * 64, "doc-v1")
    await publish_version("b" * 64, "doc-v2")

    versions, total = await sources.list_versions("versioned-source")
    assert total == 2
    assert [row.status for row in versions] == ["active", "superseded"]
    assert versions[0].version == 2
    assert versions[0].document_id == "doc-v2"
    assert versions[1].superseded_at is not None


@pytest.mark.asyncio
async def test_source_statistics_and_preset_seed(test_engine) -> None:
    assert await seed_category_preset_sources() == 9
    assert await seed_category_preset_sources() == 0
    rows = await CrawlerSourceStore().list()
    assert len([row for row in rows if row.id.startswith("preset:")]) == 9

    source = rows[0]
    crawl_job, _ = await CrawlerStore().create_job(
        knowledge_base_id=source.knowledge_base_id,
        config={"source_id": source.id},
    )
    store = CrawlerSourceStore()
    await store.register_run(
        source_id=source.id,
        crawl_job_id=crawl_job.id,
        trigger_type="manual",
    )
    await store.finish_run(
        crawl_job_id=crawl_job.id,
        status=CrawlJobStatus.SUCCEEDED,
        progress={"fetched": 10, "duplicates": 2, "not_modified": 3, "failed": 1},
    )
    await store.note_review(crawl_job_id=crawl_job.id, action="approve")
    await store.note_review(crawl_job_id=crawl_job.id, action="reject")

    stats = await store.stats(source.id)
    assert stats["success_rate"] == 1.0
    assert stats["duplicate_rate"] == pytest.approx(5 / 13, rel=1e-4)
    assert stats["approval_rate"] == 0.5
    assert stats["freshness_status"] == "fresh"


@pytest.mark.asyncio
async def test_structured_adapter_id_does_not_replace_managed_source_id(
    test_engine,
    mock_qdrant,
) -> None:
    knowledge_base = await KnowledgeBaseStore().get_default()
    sources = CrawlerSourceStore()
    await sources.create(
        {
            "id": "managed-vulnerability-source",
            "knowledge_base_id": knowledge_base.id,
            "name": "Managed vulnerability source",
            "source_kind": "custom",
            "config": {"structured_sources": ["nvd"]},
        }
    )
    crawler = CrawlerStore()
    crawl_job, _ = await crawler.create_job(
        knowledge_base_id=knowledge_base.id,
        config={
            "source_id": "managed-vulnerability-source",
            "incremental": True,
            "structured_sources": ["nvd"],
            "max_total_pages": 1,
            "min_content_chars": 10,
        },
    )
    await sources.register_run(
        source_id="managed-vulnerability-source",
        crawl_job_id=crawl_job.id,
        trigger_type="scheduled",
    )

    class Adapter:
        info = StructuredSourceInfo(
            id="nvd",
            name="NVD fixture",
            description="Regression fixture",
            mode="remote",
            default_limit=1,
        )

        async def crawl(self, *_args, **_kwargs):
            yield CrawlPage(
                url="https://nvd.nist.gov/vuln/detail/CVE-2026-8729",
                title="CVE-2026-8729",
                markdown="# CVE-2026-8729\n\nAffected versions and remediation guidance.",
                content_hash="c" * 64,
                source_type="structured",
                metadata={"source_adapter": "nvd"},
            )

    class EmptyEngine:
        async def crawl(self, *_args, **_kwargs):
            if False:
                yield

    assert await crawler.claim(crawl_job.id)
    await CrawlerRunner(
        store=crawler,
        engine=EmptyEngine(),
        structured_registry=StructuredSourceRegistry((Adapter(),)),
    ).run(crawl_job.id)

    async with db.get_engine().connect() as connection:
        result = await connection.execute(select(CrawlerResourceStateRow.source_id))
        source_ids = list(result.scalars().all())
    assert source_ids == ["managed-vulnerability-source"]
