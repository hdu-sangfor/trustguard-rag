"""Regression coverage for crawler safety, leases, and review concurrency."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from app.core.crawler.engine import CrawlEngine, CrawlPage, CrawlRequest
from app.core.embedding.profiles import get_embedding_profile
from app.core.crawler.review import apply_review, get_review
from app.core.crawler.runner import CrawlerRunner
from app.core.crawler.transport import SafeNetworkBackend
from app.domain.crawler import CrawlJobStatus
from app.settings import get_settings
from app.stores.blob_store import get_blob_store
from app.stores.crawler_store import CrawlerStore
from app.stores.knowledge_base_store import KnowledgeBaseStore
from app.stores.models import CrawlJobRow


def test_compose_worker_consumes_ordinary_ingest_queue() -> None:
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "RAG_WORKER_CONSUME_QUEUES: rag.crawl,rag.ingest," in compose


def test_committed_crawler_report_preserves_v2_quality_evidence() -> None:
    report = (
        Path(__file__).resolve().parents[1]
        / "evaluation"
        / "crawler"
        / "results"
        / "report.md"
    ).read_text(encoding="utf-8")
    assert "数据集版本：V2" in report
    assert "语料：扫描 122 个源文件，入库 102 篇" in report
    assert "查询：133 道；可回答 108，不可回答 25" in report
    assert "数据指纹：`f3f03a943dd5449e95d3fa28f36809b4c5dfc1071d86471fdf2994681ef8b3f7`" in report


@pytest.mark.asyncio
async def test_safe_network_backend_connects_to_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[str, int]] = []
    sentinel_stream = object()

    class Backend:
        async def connect_tcp(self, host: str, port: int, **kwargs):
            connected.append((host, port))
            return sentinel_stream

        async def sleep(self, seconds: float) -> None:
            return None

    async def resolve(host: str, port: int, **kwargs) -> tuple[str, ...]:
        assert host == "rebind.example"
        return ("93.184.216.34",)

    monkeypatch.setattr(
        "app.core.crawler.transport.resolve_host_addresses",
        resolve,
    )
    backend = SafeNetworkBackend(backend=Backend())  # type: ignore[arg-type]
    stream = await backend.connect_tcp("rebind.example", 443)

    assert stream is sentinel_stream
    assert connected == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_crawler_rejects_declared_oversized_response_before_body_read() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-length": "5000001",
            },
            content=b"small test body",
            request=request,
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    async def validator(url: str, **kwargs) -> str:
        return url

    errors: list[str] = []

    async def on_error(url: str, error: Exception) -> None:
        errors.append(str(error))

    pages = [
        page
        async for page in CrawlEngine(
            client_factory=client_factory,
            validator=validator,
        ).crawl(
            CrawlRequest(
                urls=["https://example.com/oversized"],
                max_total_pages=1,
                max_retries=0,
                fetch_delay_seconds=0,
            ),
            on_error=on_error,
        )
    ]

    assert pages == []
    assert errors == ["Page response exceeds 5 MB"]


@pytest.mark.asyncio
async def test_recovered_crawler_attempt_fences_old_runner(test_engine) -> None:
    store = CrawlerStore()
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={"urls": ["https://example.com/fenced"]},
    )
    assert await store.claim(row.id)
    first = await store.get(row.id)
    assert first is not None

    async with test_engine.begin() as connection:
        await connection.execute(
            update(CrawlJobRow)
            .where(CrawlJobRow.id == row.id)
            .values(
                updated_at=(
                    datetime.now(timezone.utc).replace(tzinfo=None)
                    - timedelta(hours=1)
                )
            )
        )

    assert len(await store.recover_stale_jobs()) == 1
    assert await store.claim(row.id)
    second = await store.get(row.id)
    assert second is not None and second.attempt == first.attempt + 1

    assert not await store.heartbeat(row.id, first.attempt)
    assert not await store.update_progress(
        row.id,
        progress={"fetched": 99},
        expected_attempt=first.attempt,
    )
    assert not await store.finish(
        row.id,
        CrawlJobStatus.SUCCEEDED,
        expected_attempt=first.attempt,
    )
    persisted = await store.get(row.id)
    assert persisted is not None
    assert persisted.status == CrawlJobStatus.RUNNING
    assert persisted.progress_json.get("fetched") != 99


@pytest.mark.asyncio
async def test_concurrent_review_actions_do_not_overwrite_each_other(test_engine) -> None:
    store = CrawlerStore()
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={"urls": ["https://example.com/review"]},
    )
    progress = dict(row.progress_json or {})
    progress.update(
        {
            "review_items": [
                {
                    "id": "review-one",
                    "status": "pending",
                    "knowledge_base_id": knowledge_base_id,
                    "source_uri": "https://example.com/review/one",
                    "content_hash": "1" * 64,
                },
                {
                    "id": "review-two",
                    "status": "pending",
                    "knowledge_base_id": knowledge_base_id,
                    "source_uri": "https://example.com/review/two",
                    "content_hash": "2" * 64,
                },
            ],
            "pending_review": 2,
            "review_status": "pending",
        }
    )
    await store.update_progress(row.id, progress=progress)

    await asyncio.gather(
        apply_review(row.id, action="reject", item_ids=["review-one"]),
        apply_review(row.id, action="reject", item_ids=["review-two"]),
    )

    review = await get_review(row.id)
    assert review["pending"] == 0
    assert review["rejected"] == 2
    assert {item["status"] for item in review["items"]} == {"rejected"}


@pytest.mark.asyncio
async def test_expired_review_claim_can_be_recovered(
    test_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_CRAWLER_REVIEW_CLAIM_SECONDS", "1")
    get_settings.cache_clear()
    store = CrawlerStore()
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={"urls": ["https://example.com/stale-review"]},
    )
    progress = dict(row.progress_json or {})
    progress.update(
        {
            "review_items": [
                {
                    "id": "stale-review-item",
                    "status": "processing",
                    "review_claim_token": "dead-worker",
                    "review_claimed_at": "2000-01-01T00:00:00",
                    "knowledge_base_id": knowledge_base_id,
                    "source_uri": "https://example.com/stale-review",
                    "content_hash": "b" * 64,
                }
            ],
            "pending_review": 1,
            "review_status": "pending",
        }
    )
    await store.update_progress(row.id, progress=progress)

    try:
        review = await apply_review(
            row.id,
            action="reject",
            item_ids=["stale-review-item"],
        )
    finally:
        get_settings.cache_clear()

    assert review["pending"] == 0
    assert review["rejected"] == 1


@pytest.mark.asyncio
async def test_delete_knowledge_base_cleans_crawler_review_state(
    client,
) -> None:
    knowledge_base = await KnowledgeBaseStore().create(
        name="crawler-delete-regression",
        profile=get_embedding_profile("configured"),
    )
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base.id,
        config={"urls": ["https://example.com/delete-review"]},
    )
    item_id = "delete-review-item"
    get_blob_store().put_job_upload(item_id, b"review content")
    progress = dict(row.progress_json or {})
    progress.update(
        {
            "review_items": [
                {
                    "id": item_id,
                    "status": "pending",
                    "knowledge_base_id": knowledge_base.id,
                    "source_uri": "https://example.com/delete-review",
                }
            ],
            "pending_review": 1,
            "review_status": "pending",
        }
    )
    await store.update_progress(row.id, progress=progress)
    await store.record_url(
        knowledge_base_id=knowledge_base.id,
        url="https://example.com/delete-review",
        status="pending_review",
    )

    response = await client.delete(f"/v1/knowledge-bases/{knowledge_base.id}")

    assert response.status_code == 204
    crawler = await store.get(row.id)
    assert crawler is not None and crawler.status == CrawlJobStatus.CANCELLED
    review = await get_review(row.id)
    assert review["rejected"] == 1
    with pytest.raises(FileNotFoundError):
        get_blob_store().read_job_upload(item_id)
    assert not await store.is_url_crawled(
        knowledge_base.id,
        "https://example.com/delete-review",
    )


@pytest.mark.asyncio
async def test_crawler_api_uses_settings_for_omitted_defaults(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_CRAWLER_MAX_TOTAL_PAGES", "17")
    monkeypatch.setenv("RAG_CRAWLER_MAX_RETRIES", "4")
    monkeypatch.setenv("RAG_CRAWLER_MAX_CHARS", "999")
    get_settings.cache_clear()

    async def dispatch(_event) -> None:
        return None

    monkeypatch.setattr("app.api.crawler.dispatch_eager", dispatch)
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    try:
        response = await client.post(
            "/v1/crawler/jobs",
            json={
                "knowledge_base_id": knowledge_base_id,
                "urls": ["https://example.com/settings-defaults"],
                "max_chars": 0,
            },
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 202
    config = response.json()["config"]
    assert config["max_total_pages"] == 17
    assert config["max_retries"] == 4
    assert config["max_chars"] == 0


@pytest.mark.asyncio
async def test_agent_review_rechecks_cancel_state(
    test_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CrawlerStore()
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={
            "urls": ["https://example.com/cancel-agent"],
            "require_review": True,
            "review_mode": "agent",
            "review_criteria": "Only approved security guidance is admitted.",
            "fetch_delay_seconds": 0,
        },
    )

    class Engine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            yield CrawlPage(
                url=request.urls[0],
                title="Cancel during agent review",
                markdown=(
                    "# Cancel during agent review\n\n"
                    "This security guidance is long enough to pass deterministic cleaning."
                ),
                content_hash="a" * 64,
            )

    async def cancel_during_review(job_id: str, **kwargs) -> dict[str, int]:
        await store.request_cancel(job_id)
        return {"approved": 0, "rejected": 0, "manual_review": 1, "failed": 0}

    monkeypatch.setattr(
        "app.core.crawler.agent_review.run_agent_review",
        cancel_during_review,
    )
    assert await store.claim(row.id)
    claimed = await store.get(row.id)
    assert claimed is not None
    await CrawlerRunner(store=store, engine=Engine()).run(
        row.id,
        expected_attempt=claimed.attempt,
    )

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.CANCELLED
