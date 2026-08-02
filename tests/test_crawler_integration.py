"""Crawler 安全、任务 API 与 RAG 入库集成测试。"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.core.crawler.cleaning import CrawlerCleaner
from app.core.crawler.engine import CrawlEngine, CrawlPage, CrawlRequest, extract_page
from app.core.crawler.runner import CrawlerRunner
from app.core.crawler.review import apply_review, get_review, get_review_content
from app.core.crawler.safety import UnsafeUrlError, validate_public_url
from app.core.crawler.structured import (
    CapecAdapter,
    CweAdapter,
    CweViewsAdapter,
    LegacyCorpusAdapter,
    NvdAdapter,
    StructuredSourceInfo,
    StructuredSourceRegistry,
    default_structured_registry,
)
from app.domain import IngestJobStatus
from app.domain.crawler import CrawlJobStatus
from app.stores.blob_store import get_blob_store
from app.stores.crawler_store import CrawlerStore
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore
from app.stores.knowledge_base_store import KnowledgeBaseStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.8/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
    ],
)
async def test_crawler_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await validate_public_url(url)


@pytest.mark.asyncio
async def test_crawler_retries_transient_http_response() -> None:
    responses = [
        httpx.Response(
            503,
            request=httpx.Request("GET", "https://example.com/advisory"),
        ),
        httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Advisory</title><main>Install the security update now.</main></html>",
            request=httpx.Request("GET", "https://example.com/advisory"),
        ),
    ]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return responses.pop(0)

    async def validator(url: str, **kwargs) -> str:
        return url

    pages = [
        page
        async for page in CrawlEngine(
            client_factory=lambda **kwargs: Client(),
            validator=validator,
        ).crawl(
            CrawlRequest(
                urls=["https://example.com/advisory"],
                max_total_pages=1,
                max_retries=1,
                retry_base_seconds=0,
                fetch_delay_seconds=0,
            )
        )
    ]

    assert len(pages) == 1
    assert not responses


@pytest.mark.asyncio
async def test_site_discovery_follows_redirect_and_uses_final_host() -> None:
    requested: list[str] = []
    validated: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            requested.append(url)
            if url.endswith("/old-docs"):
                return httpx.Response(
                    301,
                    headers={"location": "/new-docs/"},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                text=(
                    '<a href="/new-docs/guide">Guide</a>'
                    '<a href="https://other.example/ignored">Other</a>'
                ),
                request=httpx.Request("GET", url),
            )

    async def validator(url: str, **kwargs) -> str:
        validated.append(url)
        return url

    links = await CrawlEngine(
        client_factory=lambda **kwargs: Client(),
        validator=validator,
    )._discover_site_links(
        "https://docs.example/old-docs",
        limit=5,
        request=CrawlRequest(max_retries=0),
    )

    assert requested == [
        "https://docs.example/old-docs",
        "https://docs.example/new-docs/",
    ]
    assert validated == requested
    assert links == [
        "https://docs.example/new-docs/",
        "https://docs.example/new-docs/guide",
    ]


@pytest.mark.asyncio
async def test_crawler_isolates_keyword_discovery_failure() -> None:
    errors: list[tuple[str, str]] = []

    async def searcher(keyword: str, limit: int) -> list[str]:
        if keyword == "broken":
            raise RuntimeError("search unavailable")
        return ["https://example.com/good"]

    async def validator(url: str, **kwargs) -> str:
        return url

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><title>Good</title><main>Readable security guidance.</main></html>",
                request=httpx.Request("GET", url),
            )

    async def on_error(url: str, error: Exception) -> None:
        errors.append((url, str(error)))

    pages = [
        page
        async for page in CrawlEngine(
            client_factory=lambda **kwargs: Client(),
            validator=validator,
            searcher=searcher,
        ).crawl(
            CrawlRequest(
                keywords=["broken", "working"],
                max_total_pages=2,
                max_retries=0,
                fetch_delay_seconds=0,
            ),
            on_error=on_error,
        )
    ]

    assert [page.url for page in pages] == ["https://example.com/good"]
    assert errors == [("search:broken", "search unavailable")]


def test_crawler_extracts_security_article() -> None:
    page = extract_page(
        """
        <html><head><title>Security Advisory</title></head>
        <body><main><h1>CVE-2026-1000</h1>
        <p>Apply the security update now.</p></main></body></html>
        """,
        "https://EXAMPLE.com/advisory?utm_source=test&id=1#details",
    )
    assert page.title == "Security Advisory"
    assert page.url == "https://example.com/advisory?id=1"
    assert "CVE-2026-1000" in page.markdown


def test_crawler_cleaner_applies_owasp_markdown_rules() -> None:
    outcome = CrawlerCleaner().clean(
        CrawlPage(
            url="https://owasp.org/example",
            title="OWASP Example",
            markdown=(
                "---\ntitle: Example\n---\n"
                "# OWASP Example\n\n"
                "<strong>Attack</strong> uses hxxps://evil[.]example/payload.\n\n"
                "![diagram](https://owasp.org/diagram.png)\n\n"
                "[Prevention guidance](https://owasp.org/prevention) explains how "
                "to validate input, encode output, enforce least privilege, and "
                "monitor suspicious behavior across the application lifecycle."
            ),
            content_hash="c" * 64,
        ),
        min_content_chars=80,
    )

    assert not outcome.rejected
    assert outcome.page is not None
    assert "---\ntitle:" not in outcome.page.markdown
    assert "<strong>" not in outcome.page.markdown
    assert "![" not in outcome.page.markdown
    assert "](https://" not in outcome.page.markdown
    assert "https://evil.example/payload" in outcome.page.markdown
    assert outcome.page.metadata["cleaned"] is True
    assert outcome.page.content_hash != "c" * 64


def test_crawler_cleaner_rejects_unusable_source_records() -> None:
    rejected = CrawlerCleaner().clean(
        CrawlPage(
            url="https://nvd.nist.gov/vuln/detail/CVE-2026-0000",
            title="CVE-2026-0000",
            markdown="# CVE-2026-0000\n\nThis record must not enter the knowledge base.",
            content_hash="d" * 64,
            source_type="structured",
            metadata={
                "source_adapter": "nvd",
                "vulnerability_status": "Rejected",
            },
        )
    )
    too_short = CrawlerCleaner().clean(
        CrawlPage(
            url="https://example.com/empty",
            title="Empty",
            markdown="# Empty\n\nNo.",
            content_hash="e" * 64,
        ),
        min_content_chars=80,
    )

    assert rejected.rejected_reason == "Source record status is REJECTED"
    assert too_short.rejected
    assert "minimum is 80" in str(too_short.rejected_reason)


@pytest.mark.asyncio
async def test_crawler_api_creates_job(client, monkeypatch) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id

    class Runner:
        async def run(self, job_id: str) -> None:
            await CrawlerStore().finish(job_id, CrawlJobStatus.SUCCEEDED)

    monkeypatch.setattr(
        "app.core.crawler.runner.get_crawler_runner",
        lambda: Runner(),
    )
    response = await client.post(
        "/v1/crawler/jobs",
        json={
            "knowledge_base_id": knowledge_base_id,
            "urls": ["https://example.com/security"],
            "max_total_pages": 1,
            "fetch_delay_seconds": 0,
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    assert (await client.get("/v1/crawler/jobs")).json()["total"] == 1


@pytest.mark.asyncio
async def test_crawler_lists_structured_sources(client) -> None:
    response = await client.get("/v1/crawler/sources")

    assert response.status_code == 200
    sources = {item["id"]: item for item in response.json()["items"]}
    assert {
        "nvd",
        "cisa_kev",
        "cwe",
        "cwe_views",
        "capec",
        "owasp",
        "legacy_corpus",
        "nist",
        "china_standards",
    } <= set(sources)
    assert sources["nvd"]["mode"] == "remote"
    assert sources["nvd"]["default_limit"] == 100
    assert sources["nist"]["mode"] == "bundled"
    assert sources["owasp"]["default_limit"] == 3
    assert sources["legacy_corpus"]["mode"] == "local"


@pytest.mark.asyncio
async def test_crawler_lists_legacy_corpus_categories(client, tmp_path, monkeypatch) -> None:
    category = tmp_path / "01_policy"
    category.mkdir()
    (category / "policy.md").write_text(
        "# Policy\n\nSecurity policy.",
        encoding="utf-8",
    )
    registry = default_structured_registry(legacy_corpus_root=tmp_path)
    monkeypatch.setattr(
        "app.api.crawler.default_structured_registry",
        lambda: registry,
    )
    thread_calls: list[str] = []

    async def to_thread(function, *args):
        thread_calls.append(function.__name__)
        return function(*args)

    monkeypatch.setattr("app.api.crawler.asyncio.to_thread", to_thread)

    response = await client.get("/v1/crawler/legacy-corpus")

    assert response.status_code == 200
    assert thread_calls == ["categories"]
    assert response.json() == {
        "available": True,
        "items": [{"name": "01_policy", "document_count": 1}],
        "total_documents": 1,
        "error": None,
    }


@pytest.mark.asyncio
async def test_crawler_lists_agent_oriented_category_presets(client) -> None:
    response = await client.get("/v1/crawler/presets")

    assert response.status_code == 200
    presets = {item["id"]: item for item in response.json()["items"]}
    assert len(presets["international_security_news"]["site_urls"]) == 15
    assert len(presets["china_security_community"]["site_urls"]) == 8
    assert len(presets["vulnerability_databases"]["site_urls"]) == 3
    assert len(presets["government_security_agencies"]["site_urls"]) == 5
    assert len(presets["chinese_security_keywords"]["keywords"]) == 25
    assert len(presets["english_security_keywords"]["keywords"]) == 20
    category_presets = [item for item in presets.values() if item["kind"] == "category"]
    assert len(category_presets) == 9
    assert presets["agent_02_vulnerability_weakness"]["category_name"] == (
        "02_漏洞与弱点知识"
    )
    assert presets["agent_02_vulnerability_weakness"]["structured_sources"] == [
        "nvd",
        "cisa_kev",
        "cwe",
        "cwe_views",
    ]
    assert presets["agent_02_vulnerability_weakness"]["domain_category"] == (
        "vulnerability_weakness"
    )
    assert presets["agent_02_vulnerability_weakness"]["kb_tier"] == "cve"
    assert presets["agent_02_vulnerability_weakness"]["phases"] == [
        "THREAT_MODEL",
        "VULN_SCAN",
    ]
    assert presets["agent_02_vulnerability_weakness"]["priority"] == "P0"
    assert "https://unit42.paloaltonetworks.com/" in presets[
        "agent_08_threat_intelligence"
    ]["site_urls"]
    assert "category_02_vulnerability" not in presets


@pytest.mark.asyncio
async def test_crawler_api_expands_original_source_presets(client, monkeypatch) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id

    class Runner:
        async def run(self, job_id: str) -> None:
            await CrawlerStore().finish(job_id, CrawlJobStatus.SUCCEEDED)

    monkeypatch.setattr(
        "app.core.crawler.runner.get_crawler_runner",
        lambda: Runner(),
    )
    response = await client.post(
        "/v1/crawler/jobs",
        json={
            "knowledge_base_id": knowledge_base_id,
            "preset_ids": [
                "international_security_news",
                "chinese_security_keywords",
            ],
            "max_total_pages": 5,
            "fetch_delay_seconds": 0,
        },
    )

    assert response.status_code == 202
    config = response.json()["config"]
    assert config["preset_ids"] == [
        "international_security_news",
        "chinese_security_keywords",
    ]
    assert len(config["site_urls"]) == 15
    assert len(config["keywords"]) == 25
    assert "https://thehackernews.com/" in config["site_urls"]
    assert "提示词注入 防护 方案" in config["keywords"]


@pytest.mark.asyncio
async def test_crawler_api_expands_category_preset_and_enables_routing(client, monkeypatch) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id

    class Runner:
        async def run(self, job_id: str) -> None:
            await CrawlerStore().finish(job_id, CrawlJobStatus.SUCCEEDED)

    monkeypatch.setattr(
        "app.core.crawler.runner.get_crawler_runner",
        lambda: Runner(),
    )
    response = await client.post(
        "/v1/crawler/jobs",
        json={
            "knowledge_base_id": knowledge_base_id,
            "preset_ids": ["agent_02_vulnerability_weakness"],
            "max_total_pages": 5,
            "fetch_delay_seconds": 0,
        },
    )

    assert response.status_code == 202
    config = response.json()["config"]
    assert config["target_category"] == "02_漏洞与弱点知识"
    assert config["domain_category"] == "vulnerability_weakness"
    assert config["route_by_category"] is True
    assert config["structured_sources"] == ["nvd", "cisa_kev", "cwe", "cwe_views"]
    assert config["site_urls"] == [
        "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"
    ]
    assert "critical CVE vulnerability 2025 2026 exploitation" in config["keywords"]


@pytest.mark.asyncio
async def test_crawler_api_rejects_multiple_category_presets(client) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    response = await client.post(
        "/v1/crawler/jobs",
        json={
            "knowledge_base_id": knowledge_base_id,
            "preset_ids": [
                "agent_02_vulnerability_weakness",
                "agent_09_compliance_reporting",
            ],
        },
    )

    assert response.status_code == 422
    assert "Only one crawler category preset" in response.json()["detail"]


@pytest.mark.asyncio
async def test_crawler_api_rejects_unknown_preset(client) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    response = await client.post(
        "/v1/crawler/jobs",
        json={
            "knowledge_base_id": knowledge_base_id,
            "preset_ids": ["not-a-real-preset"],
        },
    )

    assert response.status_code == 422
    assert "Unknown crawler preset" in response.json()["detail"]


@pytest.mark.asyncio
async def test_crawler_api_accepts_structured_source_only(client, monkeypatch) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id

    class Runner:
        async def run(self, job_id: str) -> None:
            await CrawlerStore().finish(job_id, CrawlJobStatus.SUCCEEDED)

    monkeypatch.setattr(
        "app.core.crawler.runner.get_crawler_runner",
        lambda: Runner(),
    )
    response = await client.post(
        "/v1/crawler/jobs",
        json={
            "knowledge_base_id": knowledge_base_id,
            "structured_sources": ["cisa_kev"],
            "source_options": {"cisa_kev": {"limit": 10}},
            "max_total_pages": 10,
        },
    )

    assert response.status_code == 202
    assert response.json()["config"]["structured_sources"] == ["cisa_kev"]


@pytest.mark.asyncio
async def test_nvd_adapter_converts_official_json_to_page() -> None:
    class Response:
        content = b"{}"

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2026-1000",
                            "published": "2026-01-01T00:00:00Z",
                            "lastModified": "2026-01-02T00:00:00Z",
                            "vulnStatus": "Analyzed",
                            "descriptions": [{"lang": "en", "value": "Remote code execution."}],
                            "weaknesses": [{"description": [{"lang": "en", "value": "CWE-94"}]}],
                            "metrics": {
                                "cvssMetricV31": [
                                    {
                                        "cvssData": {
                                            "version": "3.1",
                                            "baseScore": 9.8,
                                            "baseSeverity": "CRITICAL",
                                            "vectorString": (
                                                "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
                                            ),
                                        }
                                    }
                                ]
                            },
                        }
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            assert "services.nvd.nist.gov" in url
            assert kwargs["params"]["resultsPerPage"] == 1
            return Response()

    adapter = NvdAdapter(client_factory=lambda **kwargs: Client())
    pages = [page async for page in adapter.crawl({"days_back": 7}, limit=1)]

    assert len(pages) == 1
    assert pages[0].source_type == "structured"
    assert pages[0].metadata["source_adapter"] == "nvd"
    assert pages[0].metadata["artifact_filename"] == "cleaned_CVE-2026-1000.md"
    assert pages[0].metadata["cvss_score"] == 9.8
    assert pages[0].title == "漏洞编号: CVE-2026-1000"
    assert "Remote code execution." in pages[0].markdown
    assert "CWE-94" in pages[0].markdown
    assert "**CVSS 评分**: 9.8" in pages[0].markdown
    assert "**严重等级**: CRITICAL" in pages[0].markdown


@pytest.mark.asyncio
async def test_capec_adapter_converts_mitre_stix_bundle() -> None:
    class Response:
        content = b"{}"

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "objects": [
                    {
                        "type": "attack-pattern",
                        "name": "SQL Injection",
                        "description": "Inject SQL control characters.",
                        "external_references": [
                            {"source_name": "capec", "external_id": "CAPEC-66"}
                        ],
                        "x_capec_likelihood_of_attack": "High",
                        "x_capec_typical_severity": "High",
                    }
                ]
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            assert "mitre/cti" in url
            return Response()

    adapter = CapecAdapter(client_factory=lambda **kwargs: Client())
    pages = [page async for page in adapter.crawl({"ids": ["CAPEC-66"]}, limit=1)]

    assert len(pages) == 1
    assert pages[0].metadata["capec_id"] == "CAPEC-66"
    assert "SQL Injection" in pages[0].markdown
    assert "Likelihood: High" in pages[0].markdown


@pytest.mark.asyncio
async def test_cwe_adapter_continues_after_one_record_fails() -> None:
    errors: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            if url.endswith("/79"):
                return httpx.Response(
                    500,
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json={
                    "Weaknesses": [
                        {
                            "Name": "Improper Neutralization",
                            "Description": "Untrusted input reaches an interpreter.",
                        }
                    ]
                },
                request=httpx.Request("GET", url),
            )

    async def on_error(url: str, error: Exception) -> None:
        errors.append(url)

    pages = [
        page
        async for page in CweAdapter(client_factory=lambda **kwargs: Client()).crawl(
            {"ids": ["CWE-79", "CWE-787"]},
            limit=2,
            max_retries=0,
            on_error=on_error,
        )
    ]

    assert [page.metadata["cwe_id"] for page in pages] == ["CWE-787"]
    assert errors == ["https://cwe-api.mitre.org/api/v1/cwe/weakness/79"]


@pytest.mark.asyncio
async def test_cwe_views_adapter_preserves_original_artifact_naming() -> None:
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            return httpx.Response(
                200,
                json={
                    "Views": [
                        {
                            "ID": "1000",
                            "Name": "Research Concepts",
                            "Type": "Graph",
                            "Status": "Stable",
                            "Objective": "Organizes weaknesses for research.",
                        }
                    ]
                },
                request=httpx.Request("GET", url),
            )

    pages = [
        page
        async for page in CweViewsAdapter(client_factory=lambda **kwargs: Client()).crawl(
            {"ids": ["1000"]}, limit=1
        )
    ]

    assert len(pages) == 1
    assert pages[0].metadata["cwe_view_id"] == "1000"
    assert pages[0].metadata["artifact_filename"] == "view_1000_Research_Concepts.md"
    assert "Organizes weaknesses for research." in pages[0].markdown


@pytest.mark.asyncio
async def test_owasp_bundled_source_restores_original_three_documents() -> None:
    adapter = default_structured_registry().get("owasp")
    pages = [page async for page in adapter.crawl({}, limit=3)]

    assert [page.metadata["artifact_filename"] for page in pages] == [
        "OWASP_Top10_2021_Detailed.md",
        "OWASP_ASVS_v4.0.3.md",
        "OWASP_WSTG_v4.2.md",
    ]
    assert [page.metadata["baseline_version"] for page in pages] == [
        "2021",
        "4.0.3",
        "4.2",
    ]
    assert "A10:2021" in pages[0].markdown
    assert "WSTG-APIT" in pages[2].markdown


@pytest.mark.asyncio
async def test_legacy_corpus_adapter_supports_category_offset_and_catalog_cache(
    tmp_path, monkeypatch
) -> None:
    first_category = tmp_path / "01_policy"
    second_category = tmp_path / "02_vulnerability"
    first_category.mkdir()
    second_category.mkdir()
    (first_category / "a.md").write_text("# Policy A\n\nControl A.", encoding="utf-8")
    (first_category / "b.md").write_text("# Policy B\n\nControl B.", encoding="utf-8")
    (second_category / "c.md").write_text(
        "# Vulnerability C\n\nPatch C.",
        encoding="utf-8",
    )
    adapter = LegacyCorpusAdapter(tmp_path)

    pages = [
        page
        async for page in adapter.crawl(
            {"category": "01_policy", "offset": 1},
            limit=1,
        )
    ]

    assert len(pages) == 1
    assert pages[0].title == "Policy B"
    assert pages[0].metadata["artifact_filename"] == "b.md"
    assert pages[0].metadata["legacy_category"] == "01_policy"
    assert pages[0].url.startswith("legacy-corpus:///01_policy/")
    original_markdown_files = adapter._markdown_files
    scan_calls = 0

    def count_scans(root, directories):
        nonlocal scan_calls
        scan_calls += 1
        return original_markdown_files(root, directories)

    monkeypatch.setattr(adapter, "_markdown_files", count_scans)
    assert adapter.categories() == [
        ("01_policy", 2),
        ("02_vulnerability", 1),
    ]
    assert adapter.categories() == [
        ("01_policy", 2),
        ("02_vulnerability", 1),
    ]
    assert scan_calls == 2
    with pytest.raises(ValueError, match="Unknown legacy corpus category"):
        _ = [
            page
            async for page in adapter.crawl(
                {"category": "../outside"},
                limit=1,
            )
        ]


@pytest.mark.asyncio
async def test_crawler_result_enters_rag_pipeline(test_engine, mock_qdrant) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={"urls": ["https://example.com/advisory"], "fetch_delay_seconds": 0},
    )

    class Engine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            yield CrawlPage(
                url=request.urls[0],
                title="Example Security Advisory",
                markdown=(
                    "# Example Security Advisory\n\n"
                    "A remotely exploitable vulnerability affects the service. "
                    "Upgrade immediately and rotate exposed credentials."
                ),
                content_hash="a" * 64,
                metadata={"crawler_parser": "test"},
            )

    assert await store.claim(row.id)
    await CrawlerRunner(store=store, engine=Engine()).run(row.id)

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    ingest_job = await JobStore().get(completed.ingest_job_ids_json[0])
    assert ingest_job is not None
    assert ingest_job.status in {
        IngestJobStatus.SUCCEEDED,
        IngestJobStatus.DEDUPLICATED,
    }
    document = await DocumentStore().get(ingest_job.document_id)
    assert document is not None
    assert document.source_type == "url"
    assert document.source_uri == "https://example.com/advisory"
    assert document.title == "Example Security Advisory"
    assert (document.metadata_json or {})["crawl_job_id"] == row.id


@pytest.mark.asyncio
async def test_agent_crawler_requires_review_before_ingest(
    test_engine,
    mock_qdrant,
) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={
            "urls": ["https://example.com/reviewed-advisory"],
            "fetch_delay_seconds": 0,
            "require_review": True,
        },
    )

    class Engine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            yield CrawlPage(
                url=request.urls[0],
                title="Reviewed security advisory",
                markdown=(
                    "# Reviewed security advisory\n\n"
                    "This cleaned advisory must remain staged until a human "
                    "reviewer explicitly approves it for RAG ingestion."
                ),
                content_hash="6" * 64,
                metadata={"crawler_parser": "test"},
            )

    assert await store.claim(row.id)
    await CrawlerRunner(store=store, engine=Engine()).run(row.id)

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    assert completed.ingest_job_ids_json == []
    review = await get_review(row.id)
    assert review["review_status"] == "pending"
    assert review["pending"] == 1
    item_id = review["items"][0]["id"]
    content = await get_review_content(row.id, item_id)
    assert "explicitly approves" in content["content"]
    assert await JobStore().get(item_id) is None

    reviewed = await apply_review(
        row.id,
        action="approve",
        item_ids=[item_id],
    )

    assert reviewed["review_status"] == "completed"
    assert reviewed["approved"] == 1
    ingest_job = await JobStore().get(item_id)
    assert ingest_job is not None
    assert ingest_job.status in {
        IngestJobStatus.SUCCEEDED,
        IngestJobStatus.DEDUPLICATED,
    }
    with pytest.raises(FileNotFoundError):
        get_blob_store().read_job_upload(item_id)
    committed_content = await get_review_content(row.id, item_id)
    assert "explicitly approves" in committed_content["content"]


@pytest.mark.asyncio
async def test_structured_crawler_result_enters_rag_pipeline(test_engine, mock_qdrant) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={
            "structured_sources": ["fixture"],
            "max_total_pages": 1,
            "fetch_delay_seconds": 0,
        },
    )

    class Adapter:
        info = StructuredSourceInfo(
            id="fixture",
            name="Fixture",
            description="Test source",
            mode="bundled",
            default_limit=1,
        )

        async def crawl(self, options, *, limit, **kwargs):
            assert limit == 1
            yield CrawlPage(
                url="https://example.com/standards/control-1",
                title="Structured Security Control",
                markdown=(
                    "# Structured Security Control\n\n"
                    "Apply least privilege and review access periodically. "
                    "Record approvals, monitor privileged sessions, and remove "
                    "stale permissions through a documented review process."
                ),
                content_hash="b" * 64,
                source_type="structured",
                metadata={
                    "source_adapter": "fixture",
                    "artifact_filename": "cleaned_TEST-1.md",
                },
            )

    class EmptyEngine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            if False:
                yield

    registry = StructuredSourceRegistry((Adapter(),))
    assert await store.claim(row.id)
    await CrawlerRunner(
        store=store,
        engine=EmptyEngine(),
        structured_registry=registry,
    ).run(row.id)

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    ingest_job = await JobStore().get(completed.ingest_job_ids_json[0])
    assert ingest_job is not None
    document = await DocumentStore().get(ingest_job.document_id)
    assert document is not None
    assert document.source_type == "structured"
    assert document.original_filename == "cleaned_TEST-1.md"
    assert (document.metadata_json or {})["source_adapter"] == "fixture"
    assert (document.metadata_json or {})["cleaned"] is True


@pytest.mark.asyncio
async def test_structured_source_reserves_capacity_for_web_sources(
    test_engine, mock_qdrant
) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={
            "structured_sources": ["fixture"],
            "urls": ["https://example.com/web-advisory"],
            "max_total_pages": 3,
            "fetch_delay_seconds": 0,
        },
    )
    limits: list[int] = []

    class Adapter:
        info = StructuredSourceInfo(
            id="fixture",
            name="Fixture",
            description="Test source",
            mode="remote",
            default_limit=100,
        )

        async def crawl(self, options, *, limit, **kwargs):
            limits.append(limit)
            for index in range(limit):
                yield CrawlPage(
                    url=f"https://example.com/structured/{index}",
                    title=f"Structured record {index}",
                    markdown=(
                        f"# Structured record {index}\n\n"
                        "This structured security record contains enough material "
                        "for deterministic cleaning and ingestion validation."
                    ),
                    content_hash=str(index + 1) * 64,
                    source_type="structured",
                    metadata={"source_adapter": "fixture"},
                )

    class Engine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            assert request.max_total_pages == 1
            yield CrawlPage(
                url=request.urls[0],
                title="Web advisory",
                markdown=(
                    "# Web advisory\n\n"
                    "This web security advisory must retain capacity after the "
                    "structured source has consumed its fair share."
                ),
                content_hash="7" * 64,
                metadata={"crawler_parser": "test"},
            )

    assert await store.claim(row.id)
    await CrawlerRunner(
        store=store,
        engine=Engine(),
        structured_registry=StructuredSourceRegistry((Adapter(),)),
    ).run(row.id)

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    assert limits == [2]
    assert completed.progress_json["source_limits"] == {"fixture": 2}
    assert len(completed.ingest_job_ids_json) == 3
    ingest_jobs = [await JobStore().get(job_id) for job_id in completed.ingest_job_ids_json]
    assert any(
        job is not None and job.source == "https://example.com/web-advisory" for job in ingest_jobs
    )


@pytest.mark.asyncio
async def test_crawler_cleaner_rejection_is_recorded_without_ingest(
    test_engine, mock_qdrant
) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={
            "structured_sources": ["fixture"],
            "max_total_pages": 1,
        },
    )

    class Adapter:
        info = StructuredSourceInfo(
            id="fixture",
            name="Fixture",
            description="Test source",
            mode="bundled",
            default_limit=1,
        )

        async def crawl(self, options, *, limit, **kwargs):
            yield CrawlPage(
                url="https://example.com/rejected-cve",
                title="Rejected CVE",
                markdown=(
                    "# Rejected CVE\n\n"
                    "This long source record was rejected upstream and must never "
                    "be indexed even though it contains enough readable content."
                ),
                content_hash="f" * 64,
                source_type="structured",
                metadata={"vulnerability_status": "REJECTED"},
            )

    class EmptyEngine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            if False:
                yield

    assert await store.claim(row.id)
    await CrawlerRunner(
        store=store,
        engine=EmptyEngine(),
        structured_registry=StructuredSourceRegistry((Adapter(),)),
    ).run(row.id)

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    assert completed.ingest_job_ids_json == []
    assert completed.progress_json["rejected"] == 1
    assert "REJECTED" in completed.progress_json["rejections"][0]["reason"]


@pytest.mark.asyncio
async def test_structured_resume_scans_original_limit_and_skips_processed_urls(
    test_engine, mock_qdrant
) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=knowledge_base_id,
        config={
            "structured_sources": ["fixture"],
            "max_total_pages": 3,
            "fetch_delay_seconds": 0,
        },
    )
    progress = dict(row.progress_json)
    progress.update({"discovered": 1, "fetched": 1, "queued_for_ingest": 1})
    await store.update_progress(row.id, progress=progress)
    await store.record_url(
        knowledge_base_id=knowledge_base_id,
        url="https://example.com/resume/1",
        status="queued_for_ingest",
    )
    limits: list[int] = []

    class Adapter:
        info = StructuredSourceInfo(
            id="fixture",
            name="Fixture",
            description="Test source",
            mode="bundled",
            default_limit=3,
        )

        async def crawl(self, options, *, limit, **kwargs):
            limits.append(limit)
            for index in range(1, 4):
                yield CrawlPage(
                    url=f"https://example.com/resume/{index}",
                    title=f"Resume record {index}",
                    markdown=(
                        f"# Resume record {index}\n\n"
                        "Apply least privilege, patch exposed services, and retain "
                        "audit evidence for security incident investigation."
                    ),
                    content_hash=str(index) * 64,
                    source_type="structured",
                    metadata={"source_adapter": "fixture"},
                )

    class EmptyEngine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            if False:
                yield

    assert await store.claim(row.id)
    await CrawlerRunner(
        store=store,
        engine=EmptyEngine(),
        structured_registry=StructuredSourceRegistry((Adapter(),)),
    ).run(row.id)

    completed = await store.get(row.id)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    assert limits == [3]
    assert completed.progress_json["fetched"] == 3
    assert completed.progress_json["skipped"] == 1
    assert len(completed.ingest_job_ids_json) == 2


@pytest.mark.asyncio
async def test_rejected_url_is_treated_as_already_processed(test_engine) -> None:
    knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    store = CrawlerStore()
    url = "https://example.com/rejected-on-resume"

    await store.record_url(
        knowledge_base_id=knowledge_base_id,
        url=url,
        status="rejected",
        error="reserved record",
    )

    assert await store.is_url_crawled(knowledge_base_id, url)


@pytest.mark.asyncio
async def test_legacy_corpus_can_route_document_to_category_knowledge_base(
    test_engine, mock_qdrant
) -> None:
    default_knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    category = f"legacy-category-{uuid4()}"
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=default_knowledge_base_id,
        config={
            "structured_sources": ["legacy_fixture"],
            "max_total_pages": 1,
            "route_by_category": True,
        },
    )

    class Adapter:
        info = StructuredSourceInfo(
            id="legacy_fixture",
            name="Legacy fixture",
            description="Test source",
            mode="local",
            default_limit=1,
        )

        async def crawl(self, options, *, limit, **kwargs):
            yield CrawlPage(
                url="legacy-corpus:///category/routed.md",
                title="Routed legacy document",
                markdown=(
                    "# Routed legacy document\n\n"
                    "This historical security document contains sufficient content "
                    "to verify category-aware knowledge base routing and ingestion."
                ),
                content_hash="9" * 64,
                source_type="structured",
                metadata={
                    "source_adapter": "legacy_corpus",
                    "legacy_category": category,
                    "artifact_filename": "routed.md",
                },
            )

    class EmptyEngine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            if False:
                yield

    assert await store.claim(row.id)
    await CrawlerRunner(
        store=store,
        engine=EmptyEngine(),
        structured_registry=StructuredSourceRegistry((Adapter(),)),
    ).run(row.id)

    completed = await store.get(row.id)
    routed_knowledge_base = await KnowledgeBaseStore().get_by_name(category)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    assert routed_knowledge_base is not None
    assert completed.progress_json["category_routes"][category] == (routed_knowledge_base.id)
    ingest_job = await JobStore().get(completed.ingest_job_ids_json[0])
    assert ingest_job is not None
    assert ingest_job.knowledge_base_id == routed_knowledge_base.id
    document = await DocumentStore().get(ingest_job.document_id)
    assert document is not None
    assert document.knowledge_base_id == routed_knowledge_base.id


@pytest.mark.asyncio
async def test_category_preset_routes_web_page_to_named_knowledge_base(
    test_engine, mock_qdrant
) -> None:
    default_knowledge_base_id = (await KnowledgeBaseStore().get_default()).id
    category = f"preset-category-{uuid4()}"
    store = CrawlerStore()
    row, _ = await store.create_job(
        knowledge_base_id=default_knowledge_base_id,
        config={
            "urls": ["https://example.com/category-advisory"],
            "max_total_pages": 1,
            "route_by_category": True,
            "target_category": category,
            "domain_category": "detection_exploit_validation",
            "kb_tier": "manual",
            "agent_phases": ["VULN_SCAN", "EXPLOIT"],
            "topic_tags": ["poc", "evidence", "false_positive"],
            "category_priority": "P0",
            "preset_ids": ["agent_03_detection_exploit_validation"],
            "fetch_delay_seconds": 0,
        },
    )

    class Engine:
        async def crawl(self, request: CrawlRequest, **kwargs):
            assert not await kwargs["should_skip"](request.urls[0])
            yield CrawlPage(
                url=request.urls[0],
                title="Category advisory",
                markdown=(
                    "# Category advisory\n\n"
                    "This categorized advisory contains enough security guidance "
                    "to verify web crawler routing into the named knowledge base."
                ),
                content_hash="8" * 64,
                metadata={"crawler_parser": "test"},
            )

    assert await store.claim(row.id)
    await CrawlerRunner(store=store, engine=Engine()).run(row.id)

    completed = await store.get(row.id)
    target = await KnowledgeBaseStore().get_by_name(category)
    assert completed is not None
    assert completed.status == CrawlJobStatus.SUCCEEDED
    assert target is not None
    ingest_job = await JobStore().get(completed.ingest_job_ids_json[0])
    assert ingest_job is not None
    assert ingest_job.knowledge_base_id == target.id
    document = await DocumentStore().get(ingest_job.document_id)
    assert document is not None
    assert document.metadata_json["domain_category"] == ("detection_exploit_validation")
    assert document.metadata_json["kb_tier"] == "manual"
    assert document.metadata_json["agent_phases"] == ["VULN_SCAN", "EXPLOIT"]
    assert document.metadata_json["topic_tags"] == [
        "poc",
        "evidence",
        "false_positive",
    ]
    assert document.metadata_json["category_priority"] == "P0"
    assert document.metadata_json["crawler_preset_ids"] == ["agent_03_detection_exploit_validation"]
    assert await store.is_url_crawled(
        target.id,
        "https://example.com/category-advisory",
    )
