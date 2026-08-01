"""Structured cybersecurity source adapters for the crawler pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.crawler.engine import CrawlPage

StructuredErrorCallback = Callable[[str, Exception], Awaitable[None]]
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MAX_RETRY_DELAY_SECONDS = 60.0
_LEGACY_CATALOG_TTL_SECONDS = 60.0
_LEGACY_CATALOG_CACHE: dict[str, tuple[float, tuple[tuple[str, int], ...]]] = {}
_LEGACY_CATALOG_CACHE_LOCK = threading.Lock()

CWE_TOP_25 = (
    "79",
    "787",
    "89",
    "352",
    "22",
    "125",
    "78",
    "416",
    "862",
    "434",
    "20",
    "94",
    "287",
    "476",
    "502",
    "77",
    "119",
    "200",
    "918",
    "863",
    "401",
    "1321",
    "611",
    "798",
    "295",
)

CAPEC_TOP_20 = (
    "66",
    "1",
    "7",
    "19",
    "12",
    "16",
    "49",
    "59",
    "60",
    "63",
    "86",
    "242",
    "88",
    "108",
    "101",
    "220",
    "233",
    "272",
    "466",
    "664",
)

CWE_VIEWS = (
    ("1000", "Research Concepts"),
    ("1003", "Weaknesses for Simplified Mapping of Published Vulnerabilities"),
    ("699", "Software Development"),
)


@dataclass(frozen=True, slots=True)
class StructuredSourceInfo:
    id: str
    name: str
    description: str
    mode: str
    default_limit: int


class StructuredSourceAdapter(Protocol):
    info: StructuredSourceInfo

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]: ...


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        return "\n".join(
            f"- {key}: {_text(item)}" for key, item in value.items() if item is not None
        )
    return str(value)


def _page(
    *,
    url: str,
    title: str,
    body: str,
    adapter: str,
    metadata: Mapping[str, object] | None = None,
) -> CrawlPage:
    markdown = f"# {title}\n\n> 来源：{url}\n\n{body.strip()}".strip()
    return CrawlPage(
        url=url,
        title=title[:512],
        markdown=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        source_type="structured",
        metadata={
            "crawler_parser": "structured",
            "source_adapter": adapter,
            "source_url": url,
            **dict(metadata or {}),
        },
    )


class _HttpAdapter:
    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self._client_factory = client_factory or httpx.AsyncClient

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
    ) -> Any:
        async with self._client_factory(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "TrustGuardCrawler/1.0"},
        ) as client:
            for attempt in range(max_retries + 1):
                try:
                    response = await client.get(url, params=params)
                except httpx.TransportError:
                    if attempt >= max_retries:
                        raise
                    await asyncio.sleep(
                        min(max(retry_base_seconds, 0.0) * (2**attempt), 60.0)
                    )
                    continue
                status_code = int(getattr(response, "status_code", 200))
                if status_code in _RETRYABLE_STATUS_CODES and attempt < max_retries:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = float(retry_after) if retry_after else (
                            max(retry_base_seconds, 0.0) * (2**attempt)
                        )
                    except ValueError:
                        delay = max(retry_base_seconds, 0.0) * (2**attempt)
                    await asyncio.sleep(min(max(delay, 0.0), _MAX_RETRY_DELAY_SECONDS))
                    continue
                response.raise_for_status()
                if len(response.content) > 25_000_000:
                    raise ValueError("Structured source response exceeds 25 MB")
                return response.json()
        raise RuntimeError("Structured source retry loop exited unexpectedly")


def _nvd_cvss(metrics: Mapping[str, Any]) -> tuple[Any, str, str, str]:
    for key, version in (
        ("cvssMetricV40", "4.0"),
        ("cvssMetricV31", "3.1"),
        ("cvssMetricV30", "3.0"),
        ("cvssMetricV2", "2.0"),
    ):
        rows = metrics.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        row = rows[0] if isinstance(rows[0], dict) else {}
        data = row.get("cvssData") if isinstance(row.get("cvssData"), dict) else {}
        score = data.get("baseScore")
        severity = _text(data.get("baseSeverity") or row.get("baseSeverity")) or "UNKNOWN"
        vector = _text(data.get("vectorString"))
        if score is not None:
            return score, severity, version, vector
    return None, "UNKNOWN", "", ""


class NvdAdapter(_HttpAdapter):
    info = StructuredSourceInfo(
        id="nvd",
        name="NVD CVE",
        description="NIST National Vulnerability Database recent CVEs",
        mode="remote",
        default_limit=100,
    )
    endpoint = "https://services.nvd.nist.gov/rest/json/cves/2.0"

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        days_back = min(max(int(options.get("days_back", 7)), 1), 120)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)
        timestamp = "%Y-%m-%dT%H:%M:%S.000+00:00"
        data = await self._get_json(
            self.endpoint,
            params={
                "pubStartDate": start.strftime(timestamp),
                "pubEndDate": end.strftime(timestamp),
                "resultsPerPage": min(limit, 2000),
            },
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
        for item in list(data.get("vulnerabilities") or [])[:limit]:
            cve = item.get("cve") or {}
            cve_id = _text(cve.get("id")) or "unknown"
            descriptions = cve.get("descriptions") or []
            description = next(
                (_text(row.get("value")) for row in descriptions if row.get("lang") == "en"),
                _text(descriptions[0].get("value")) if descriptions else "",
            )
            weaknesses = [
                _text(desc.get("value"))
                for weakness in cve.get("weaknesses") or []
                for desc in weakness.get("description") or []
                if desc.get("value")
            ]
            metrics = cve.get("metrics") or {}
            base_score, severity, cvss_version, vector = _nvd_cvss(metrics)
            body = (
                f"## 漏洞描述\n\n{description or '暂无描述。'}\n\n"
                "## 风险评估\n\n"
                f"- **CVSS 评分**: {base_score if base_score is not None else '暂无评分'}\n"
                f"- **严重等级**: {severity}\n"
                f"- **CVSS 版本**: {cvss_version or '未知'}\n"
                f"- **攻击向量**: {vector or '暂无'}\n\n"
                "## 关联弱点类型 (CWE)\n\n"
                f"{', '.join(dict.fromkeys(weaknesses)) or '未分类'}\n\n"
                "## NVD 元数据\n\n"
                f"- **发布时间**: {_text(cve.get('published')) or '未知'}\n"
                f"- **最后修改**: {_text(cve.get('lastModified')) or '未知'}\n"
                f"- **记录状态**: {_text(cve.get('vulnStatus')) or '未知'}"
            )
            url = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
            yield _page(
                url=url,
                title=f"漏洞编号: {cve_id}",
                body=body,
                adapter=self.info.id,
                metadata={
                    "artifact_filename": f"cleaned_{cve_id}.md",
                    "cve_id": cve_id,
                    "cvss_score": base_score,
                    "cvss_severity": severity,
                    "cvss_version": cvss_version or None,
                    "cvss_vector": vector or None,
                    "published": cve.get("published"),
                    "vulnerability_status": cve.get("vulnStatus"),
                },
            )


class CisaKevAdapter(_HttpAdapter):
    info = StructuredSourceInfo(
        id="cisa_kev",
        name="CISA KEV",
        description="CISA Known Exploited Vulnerabilities catalog",
        mode="remote",
        default_limit=100,
    )
    endpoint = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        data = await self._get_json(
            self.endpoint,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
        rows = sorted(
            data.get("vulnerabilities") or [],
            key=lambda row: str(row.get("dateAdded") or ""),
            reverse=True,
        )
        for row in rows[:limit]:
            cve_id = _text(row.get("cveID")) or "unknown"
            body = (
                f"## {_text(row.get('vulnerabilityName')) or cve_id}\n\n"
                f"{_text(row.get('shortDescription'))}\n\n"
                "## Catalog metadata\n\n"
                f"- Vendor/project: {_text(row.get('vendorProject'))}\n"
                f"- Product: {_text(row.get('product'))}\n"
                f"- Date added: {_text(row.get('dateAdded'))}\n"
                f"- Required action: {_text(row.get('requiredAction'))}\n"
                f"- Due date: {_text(row.get('dueDate'))}\n"
                f"- Known ransomware use: {_text(row.get('knownRansomwareCampaignUse'))}"
            )
            url = f"https://www.cisa.gov/known-exploited-vulnerabilities-catalog?search_api_fulltext={cve_id}"
            yield _page(
                url=url,
                title=f"CISA KEV: {cve_id}",
                body=body,
                adapter=self.info.id,
                metadata={"cve_id": cve_id, "date_added": row.get("dateAdded")},
            )


class CweAdapter(_HttpAdapter):
    info = StructuredSourceInfo(
        id="cwe",
        name="MITRE CWE Top 25",
        description="MITRE weakness records, defaulting to the CWE Top 25",
        mode="remote",
        default_limit=25,
    )
    endpoint = "https://cwe-api.mitre.org/api/v1/cwe/weakness/{item_id}"

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        ids = _selected_ids(options, CWE_TOP_25, "CWE-")[:limit]
        for item_id in ids:
            source_url = self.endpoint.format(item_id=item_id)
            try:
                data = await self._get_json(
                    source_url,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                )
            except (httpx.HTTPError, ValueError, TypeError) as error:
                if on_error:
                    await on_error(source_url, error)
                continue
            rows = data.get("Weaknesses") if isinstance(data, dict) else data
            row = (rows or [{}])[0]
            name = _text(row.get("Name") or row.get("name"))
            body = (
                f"## {name or f'CWE-{item_id}'}\n\n"
                f"{_text(row.get('Description') or row.get('description'))}\n\n"
                "## Extended description\n\n"
                f"{_text(row.get('ExtendedDescription') or row.get('extended_description'))}\n\n"
                "## Applicable platforms\n\n"
                f"{_text(row.get('ApplicablePlatforms') or row.get('applicable_platforms'))}\n\n"
                "## Common consequences\n\n"
                f"{_text(row.get('CommonConsequences') or row.get('common_consequences'))}\n\n"
                "## Potential mitigations\n\n"
                f"{_text(row.get('PotentialMitigations') or row.get('potential_mitigations'))}\n\n"
                "## Detection methods\n\n"
                f"{_text(row.get('DetectionMethods') or row.get('detection_methods'))}\n\n"
                "## Related attack patterns (CAPEC)\n\n"
                f"{_text(row.get('RelatedAttackPatterns') or row.get('related_attack_patterns'))}"
            )
            yield _page(
                url=f"https://cwe.mitre.org/data/definitions/{item_id}.html",
                title=f"CWE-{item_id}: {name}".rstrip(": "),
                body=body,
                adapter=self.info.id,
                metadata={
                    "artifact_filename": f"cleaned_CWE-{item_id}.md",
                    "cwe_id": f"CWE-{item_id}",
                    "record_status": row.get("Status") or row.get("status"),
                    "likelihood_of_exploit": (
                        row.get("LikelihoodOfExploit")
                        or row.get("likelihood_of_exploit")
                    ),
                },
            )


class CweViewsAdapter(_HttpAdapter):
    info = StructuredSourceInfo(
        id="cwe_views",
        name="MITRE CWE Views",
        description="MITRE CWE research, vulnerability-mapping, and development views",
        mode="remote",
        default_limit=3,
    )
    endpoint = "https://cwe-api.mitre.org/api/v1/cwe/view/{item_id}"

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        names = dict(CWE_VIEWS)
        ids = _selected_ids(options, tuple(names), "CWE-")[:limit]
        for item_id in ids:
            source_url = self.endpoint.format(item_id=item_id)
            try:
                data = await self._get_json(
                    source_url,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                )
                rows = data.get("Views") if isinstance(data, dict) else data
                row = (rows or [{}])[0]
                name = _text(row.get("Name") or row.get("name")) or names.get(
                    item_id, f"View {item_id}"
                )
                body = (
                    f"## {name}\n\n"
                    f"{_text(row.get('Objective') or row.get('objective'))}\n\n"
                    "## View metadata\n\n"
                    f"- Type: {_text(row.get('Type') or row.get('type')) or 'Unknown'}\n"
                    f"- Status: {_text(row.get('Status') or row.get('status')) or 'Unknown'}"
                )
            except (httpx.HTTPError, ValueError, TypeError, IndexError) as error:
                if on_error:
                    await on_error(source_url, error)
                continue
            filename_name = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
            yield _page(
                url=f"https://cwe.mitre.org/data/definitions/{item_id}.html",
                title=f"CWE View {item_id}: {name}",
                body=body,
                adapter=self.info.id,
                metadata={
                    "artifact_filename": f"view_{item_id}_{filename_name}.md",
                    "cwe_view_id": item_id,
                    "record_status": row.get("Status") or row.get("status"),
                },
            )


class CapecAdapter(_HttpAdapter):
    info = StructuredSourceInfo(
        id="capec",
        name="MITRE CAPEC",
        description="MITRE CAPEC STIX 2.1 common attack pattern records",
        mode="remote",
        default_limit=20,
    )
    endpoint = "https://raw.githubusercontent.com/mitre/cti/master/capec/2.1/stix-capec.json"

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        ids = _selected_ids(options, CAPEC_TOP_20, "CAPEC-")[:limit]
        data = await self._get_json(
            self.endpoint,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
        patterns: dict[str, dict[str, Any]] = {}
        for item in data.get("objects") or []:
            if item.get("type") != "attack-pattern":
                continue
            external_id = next(
                (
                    _text(reference.get("external_id"))
                    for reference in item.get("external_references") or []
                    if str(reference.get("external_id") or "").startswith("CAPEC-")
                ),
                "",
            )
            if external_id:
                patterns[external_id.removeprefix("CAPEC-")] = item
        for item_id in ids:
            row = patterns.get(item_id)
            if row is None:
                continue
            name = _text(row.get("name"))
            body = (
                f"## {name or f'CAPEC-{item_id}'}\n\n"
                f"{_text(row.get('description'))}\n\n"
                "## Attack characteristics\n\n"
                f"- Likelihood: {_text(row.get('x_capec_likelihood_of_attack'))}\n"
                f"- Typical severity: {_text(row.get('x_capec_typical_severity'))}\n\n"
                f"## Execution flow\n\n{_text(row.get('x_capec_execution_flow'))}"
            )
            yield _page(
                url=f"https://capec.mitre.org/data/definitions/{item_id}.html",
                title=f"CAPEC-{item_id}: {name}".rstrip(": "),
                body=body,
                adapter=self.info.id,
                metadata={
                    "artifact_filename": f"cleaned_CAPEC-{item_id}.md",
                    "capec_id": f"CAPEC-{item_id}",
                    "deprecated": bool(row.get("x_mitre_deprecated")),
                    "revoked": bool(row.get("revoked")),
                },
            )


def _selected_ids(options: Mapping[str, Any], defaults: tuple[str, ...], prefix: str) -> list[str]:
    raw_ids = options.get("ids")
    if not isinstance(raw_ids, list):
        return list(defaults)
    cleaned = [
        str(value).strip().upper().removeprefix(prefix) for value in raw_ids if str(value).strip()
    ]
    return list(dict.fromkeys(cleaned)) or list(defaults)


@dataclass(frozen=True, slots=True)
class _StaticDocument:
    slug: str
    title: str
    url: str
    body: str
    artifact_filename: str | None = None
    version: str | None = None


class StaticBaselineAdapter:
    def __init__(self, info: StructuredSourceInfo, documents: tuple[_StaticDocument, ...]) -> None:
        self.info = info
        self._documents = documents

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        for document in self._documents[:limit]:
            metadata: dict[str, object] = {"baseline_id": document.slug}
            if document.artifact_filename:
                metadata["artifact_filename"] = document.artifact_filename
            if document.version:
                metadata["baseline_version"] = document.version
            yield _page(
                url=document.url,
                title=document.title,
                body=document.body,
                adapter=self.info.id,
                metadata=metadata,
            )


class LegacyCorpusAdapter:
    info = StructuredSourceInfo(
        id="legacy_corpus",
        name="TrustGuard Legacy Markdown Corpus",
        description=(
            "Paged import of the original crawler knowledge_bases Markdown corpus"
        ),
        mode="local",
        default_limit=200,
    )

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def categories(self) -> list[tuple[str, int]]:
        root = self._resolved_root()
        cache_key = str(root)
        now = time.monotonic()
        with _LEGACY_CATALOG_CACHE_LOCK:
            cached = _LEGACY_CATALOG_CACHE.get(cache_key)
            if cached and now - cached[0] < _LEGACY_CATALOG_TTL_SECONDS:
                return list(cached[1])
        directories = self._category_directories(root)
        rows: list[tuple[str, int]] = []
        for name, directory in directories.items():
            count = len(self._markdown_files(root, [directory]))
            if count:
                rows.append((name, count))
        with _LEGACY_CATALOG_CACHE_LOCK:
            _LEGACY_CATALOG_CACHE[cache_key] = (now, tuple(rows))
        return rows

    async def crawl(
        self,
        options: Mapping[str, Any],
        *,
        limit: int,
        max_retries: int = 2,
        retry_base_seconds: float = 1.0,
        on_error: StructuredErrorCallback | None = None,
    ) -> AsyncIterator[CrawlPage]:
        root = self._resolved_root()
        category = str(options.get("category") or "").strip()
        offset = max(int(options.get("offset", 0)), 0)
        category_dirs = self._category_directories(root)
        if category:
            if category not in category_dirs:
                raise ValueError(f"Unknown legacy corpus category: {category}")
            directories = [category_dirs[category]]
            files = self._markdown_files(root, directories)
            if not files:
                raise ValueError(
                    f"Legacy corpus category contains no Markdown: {category}"
                )
        else:
            directories = [
                category_dirs[name]
                for name in sorted(category_dirs, key=str.casefold)
            ]
            files = self._markdown_files(root, directories)
        for path in files[offset : offset + limit]:
            relative = path.relative_to(root)
            source_url = f"legacy-corpus:///{quote(relative.as_posix())}"
            try:
                if path.stat().st_size > 5_000_000:
                    raise ValueError("Legacy corpus document exceeds 5 MB")
                markdown = path.read_text(encoding="utf-8")
                if not markdown.strip():
                    raise ValueError("Legacy corpus document is empty")
            except (OSError, UnicodeError, ValueError) as error:
                if on_error:
                    await on_error(source_url, error)
                continue
            heading = re.search(r"(?m)^#\s+(.+?)\s*$", markdown)
            title = heading.group(1).strip() if heading else path.stem
            yield CrawlPage(
                url=source_url,
                title=title[:512],
                markdown=markdown.strip(),
                content_hash=hashlib.sha256(
                    markdown.strip().encode("utf-8")
                ).hexdigest(),
                source_type="structured",
                metadata={
                    "crawler_parser": "legacy_markdown",
                    "source_adapter": self.info.id,
                    "source_url": source_url,
                    "artifact_filename": path.name,
                    "legacy_category": relative.parts[0],
                    "legacy_relative_path": relative.as_posix(),
                },
            )

    def _resolved_root(self) -> Path:
        try:
            root = self._root.expanduser().resolve(strict=True)
        except OSError as error:
            raise ValueError(
                f"Legacy corpus root is unavailable: {self._root}"
            ) from error
        if not root.is_dir():
            raise ValueError(f"Legacy corpus root is not a directory: {root}")
        return root

    @staticmethod
    def _category_directories(root: Path) -> dict[str, Path]:
        rows: list[tuple[str, Path]] = []
        for item in root.iterdir():
            try:
                resolved = item.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_dir() and resolved.is_relative_to(root):
                rows.append((item.name, item))
        return dict(sorted(rows, key=lambda row: row[0].casefold()))

    @staticmethod
    def _markdown_files(root: Path, directories: list[Path]) -> list[Path]:
        files: list[Path] = []
        for directory in directories:
            for path in directory.rglob("*.md"):
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                if (
                    path.is_file()
                    and resolved.is_relative_to(root)
                ):
                    files.append(path)
        return sorted(
            files,
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )


def _owasp_top10_body() -> str:
    risks = (
        (
            "A01:2021",
            "Broken Access Control（失效的访问控制）",
            "访问控制失效会导致未授权的信息泄露、修改或数据破坏。",
            "CWE-200, CWE-201, CWE-352",
            "默认拒绝访问；在服务端统一实施访问控制；记录并监控访问控制失败。",
        ),
        (
            "A02:2021",
            "Cryptographic Failures（加密失败）",
            "密码学相关失败通常导致敏感数据泄露或系统被攻破。",
            "CWE-259, CWE-327, CWE-331",
            "加密传输中和存储的数据；采用强算法与自适应密码哈希；禁用弱协议。",
        ),
        (
            "A03:2021",
            "Injection（注入）",
            "不可信数据被作为命令或查询的一部分发送到解释器时会产生注入漏洞。",
            "CWE-79, CWE-89, CWE-73",
            "使用参数化查询；执行允许列表验证；避免拼接命令并正确编码输出。",
        ),
        (
            "A04:2021",
            "Insecure Design（不安全设计）",
            "设计和架构缺陷需要通过威胁建模、安全设计模式和参考架构治理。",
            "CWE-209, CWE-256, CWE-272",
            "实施安全开发生命周期；执行威胁建模；用测试覆盖安全需求。",
        ),
        (
            "A05:2021",
            "Security Misconfiguration（安全配置错误）",
            "不安全默认值、临时配置和不必要功能会扩大攻击面。",
            "CWE-16, CWE-611",
            "自动化加固部署；最小化平台；持续审查并验证配置。",
        ),
        (
            "A06:2021",
            "Vulnerable and Outdated Components（易受攻击和过时的组件）",
            "使用存在已知漏洞的组件可能导致数据丢失或服务器被接管。",
            "CWE-1104",
            "维护 SBOM；持续监控漏洞公告；验证来源和签名；及时升级组件。",
        ),
        (
            "A07:2021",
            "Identification and Authentication Failures（身份识别和认证失败）",
            "身份管理、认证和会话管理缺陷可能导致账户被接管。",
            "CWE-287, CWE-384",
            "启用 MFA；禁用默认凭据；限制登录尝试并安全管理会话。",
        ),
        (
            "A08:2021",
            "Software and Data Integrity Failures（软件和数据完整性故障）",
            "未验证更新、关键数据和 CI/CD 管道完整性会引入供应链风险。",
            "CWE-502",
            "验证软件数字签名；保护 CI/CD 管道；验证依赖项和更新来源。",
        ),
        (
            "A09:2021",
            "Security Logging and Monitoring Failures（安全日志和监控失败）",
            "日志、监控和事件响应不足会使攻击者长期隐藏并持续攻击。",
            "CWE-778",
            "记录认证和访问控制失败；集中分析日志；建立告警并演练响应。",
        ),
        (
            "A10:2021",
            "Server-Side Request Forgery（服务端请求伪造）",
            "应用未验证远程资源 URL 时，攻击者可迫使服务访问非预期目标。",
            "CWE-918",
            "使用目标允许列表；禁用不必要的重定向；实施网络分段和出口控制。",
        ),
    )
    sections = [
        (
            f"## {risk_id}: {name}\n\n"
            f"### 风险描述\n\n{description}\n\n"
            f"### 关联弱点类型 (CWE)\n\n{cwes}\n\n"
            f"### 预防措施\n\n{prevention}"
        )
        for risk_id, name, description, cwes, prevention in risks
    ]
    return (
        "## Web 应用安全十大风险\n\n"
        "OWASP Top 10 是 Web 应用安全风险意识文档。此内置基线保留原爬虫采用的 "
        "2021 正式版本，并明确标记版本，避免与后续版本混淆。\n\n"
        + "\n\n---\n\n".join(sections)
    )


def _owasp_catalog_body(
    introduction: str,
    sections: tuple[tuple[str, str], ...],
    objective_label: str,
) -> str:
    rows = [
        f"## {section_id}: {name}\n\n### {objective_label}\n\n"
        f"围绕 {name} 执行系统化安全验证并保留证据。"
        for section_id, name in sections
    ]
    return f"{introduction}\n\n" + "\n\n---\n\n".join(rows)


OWASP_DOCUMENTS = (
    _StaticDocument(
        slug="owasp-top10-2021",
        title="OWASP Top 10:2021 - Web Application Security Risks",
        url="https://owasp.org/Top10/",
        body=_owasp_top10_body(),
        artifact_filename="OWASP_Top10_2021_Detailed.md",
        version="2021",
    ),
    _StaticDocument(
        slug="owasp-asvs-4.0.3",
        title="OWASP ASVS (Application Security Verification Standard) v4.0.3",
        url="https://owasp.org/www-project-application-security-verification-standard/",
        body=_owasp_catalog_body(
            "## 应用安全验证标准\n\n"
            "OWASP ASVS 提供应用程序安全需求和控制的验证框架。",
            (
                ("V1", "Architecture, Design and Threat Modeling"),
                ("V2", "Authentication Verification Requirements"),
                ("V3", "Session Management Verification Requirements"),
                ("V4", "Access Control Verification Requirements"),
                ("V5", "Validation, Sanitization and Encoding"),
                ("V6", "Stored Cryptography"),
                ("V7", "Error Handling and Logging"),
                ("V8", "Data Protection"),
                ("V9", "Communications"),
                ("V10", "Malicious Code"),
                ("V11", "Business Logic"),
                ("V12", "Files and Resources"),
                ("V13", "API and Web Service"),
                ("V14", "Configuration"),
            ),
            "安全验证目标",
        ),
        artifact_filename="OWASP_ASVS_v4.0.3.md",
        version="4.0.3",
    ),
    _StaticDocument(
        slug="owasp-wstg-4.2",
        title="OWASP WSTG (Web Security Testing Guide) v4.2",
        url="https://owasp.org/www-project-web-security-testing-guide/",
        body=_owasp_catalog_body(
            "## Web 安全测试指南\n\n"
            "OWASP WSTG 覆盖从信息收集到客户端与 API 测试的完整流程。",
            (
                ("WSTG-INFO", "Information Gathering（信息收集）"),
                ("WSTG-CONF", "Configuration and Deployment Management Testing"),
                ("WSTG-IDNT", "Identity Management Testing"),
                ("WSTG-ATHN", "Authentication Testing"),
                ("WSTG-ATHZ", "Authorization Testing"),
                ("WSTG-SESS", "Session Management Testing"),
                ("WSTG-INPV", "Input Validation Testing"),
                ("WSTG-ERRH", "Error Handling"),
                ("WSTG-CRYP", "Cryptography"),
                ("WSTG-BUSL", "Business Logic Testing"),
                ("WSTG-CLIENT", "Client-side Testing"),
                ("WSTG-APIT", "API Testing"),
            ),
            "测试目标",
        ),
        artifact_filename="OWASP_WSTG_v4.2.md",
        version="4.2",
    ),
)


NIST_DOCUMENTS = (
    _StaticDocument(
        slug="nist-csf-2.0",
        title="NIST Cybersecurity Framework 2.0 Baseline",
        url="https://www.nist.gov/cyberframework",
        body="""## Version

NIST CSF 2.0, published February 2024.

## Core functions

- Govern: establish cybersecurity risk strategy, policy, roles, and oversight.
- Identify: understand assets, suppliers, vulnerabilities, and business risk.
- Protect: apply safeguards such as identity, access, awareness, and data security.
- Detect: discover and analyze anomalies and adverse events.
- Respond: manage incidents, communications, mitigation, and reporting.
- Recover: restore services, validate recovery, and communicate progress.

Use Profiles to describe current and target outcomes, and Tiers to characterize the
rigor of cybersecurity risk governance and management practices.""",
    ),
    _StaticDocument(
        slug="nist-sp800-53-r5",
        title="NIST SP 800-53 Revision 5 Baseline",
        url="https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final",
        body="""## Version

NIST SP 800-53 Revision 5, including Update 1.

## Control families

Access Control; Awareness and Training; Audit and Accountability; Assessment,
Authorization and Monitoring; Configuration Management; Contingency Planning;
Identification and Authentication; Incident Response; Maintenance; Media Protection;
Physical and Environmental Protection; Planning; Program Management; Personnel
Security; PII Processing and Transparency; Risk Assessment; System and Services
Acquisition; System and Communications Protection; System and Information Integrity;
and Supply Chain Risk Management.

Controls must be tailored to system context, impact level, legal obligations, threat
model, and organization-defined parameters.""",
    ),
)

CHINA_DOCUMENTS = (
    _StaticDocument(
        slug="china-cybersecurity-law-system",
        title="中国网络与数据安全法律体系基线",
        url="https://www.cac.gov.cn/",
        body="""## 使用范围

用于 RAG 检索的法规导航基线，正式合规判断应以国家机关公布的现行文本为准。

## 核心法律与制度

- 《中华人民共和国网络安全法》：网络运行安全、关键信息基础设施和网络信息安全。
- 《中华人民共和国数据安全法》：数据分类分级、重要数据保护和跨境风险。
- 《中华人民共和国个人信息保护法》：处理规则、敏感个人信息、个人权利与跨境提供。
- 网络安全等级保护制度：定级、备案、建设整改、测评和持续监督。
- 数据出境制度：安全评估、标准合同、个人信息保护认证等适用路径。

实施时应记录适用主体、数据类型、处理目的、地域、数量、保存期限和接收方。""",
    ),
    _StaticDocument(
        slug="china-mlps",
        title="网络安全等级保护实施基线",
        url="https://www.mps.gov.cn/",
        body="""## 生命周期

定级、备案、差距分析、安全建设整改、等级测评、监督检查和持续改进。

## 控制域

安全物理环境、安全通信网络、安全区域边界、安全计算环境、安全管理中心，
以及安全管理制度、机构、人员、建设和运维管理。

云计算、移动互联、物联网、工业控制和大数据场景需要结合扩展要求。""",
    ),
    _StaticDocument(
        slug="china-data-lifecycle",
        title="数据安全生命周期控制基线",
        url="https://www.cac.gov.cn/",
        body="""## 生命周期控制

- 采集：合法、正当、必要，记录来源和授权依据。
- 传输：身份鉴别、链路加密、完整性与跨域控制。
- 存储：分类分级、最小权限、密钥管理、备份与恢复。
- 使用：目的限制、访问审计、脱敏和异常检测。
- 共享与出境：尽职调查、合同约束、风险评估和持续监督。
- 删除与销毁：期限管理、可验证删除和介质处置。

重要数据和个人信息应建立目录、责任人、处理活动记录与事件响应机制。""",
    ),
    _StaticDocument(
        slug="china-ai-security",
        title="生成式人工智能安全治理基线",
        url="https://www.cac.gov.cn/",
        body="""## 治理重点

明确服务提供者与使用者责任，管理训练数据合法性、个人信息与知识产权，
对模型安全、内容安全、算法偏见、幻觉、提示注入和供应链风险开展评估。

## 工程控制

训练与评测数据治理、模型和插件权限隔离、输入输出过滤、敏感操作人工确认、
红队测试、日志留存、投诉举报、事件响应以及版本变更评估。""",
    ),
    _StaticDocument(
        slug="china-cloud-security",
        title="云计算安全责任与控制基线",
        url="https://www.tc260.org.cn/",
        body="""## 共享责任

云服务商负责基础设施和平台约定范围内的安全，云租户负责身份、配置、数据、
工作负载和业务应用安全；责任边界必须通过合同和控制矩阵明确。

## 关键控制

租户隔离、强身份认证、最小权限、配置基线、密钥托管、日志集中化、备份恢复、
供应链管理、漏洞处置、可用性设计、退出与数据迁移。""",
    ),
)


def default_structured_registry(
    *,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
    legacy_corpus_root: str | Path | None = None,
) -> "StructuredSourceRegistry":
    if legacy_corpus_root is None:
        from app.settings import get_settings

        legacy_corpus_root = get_settings().crawler_legacy_corpus_root
    return StructuredSourceRegistry(
        (
            NvdAdapter(client_factory=client_factory),
            CisaKevAdapter(client_factory=client_factory),
            CweAdapter(client_factory=client_factory),
            CweViewsAdapter(client_factory=client_factory),
            CapecAdapter(client_factory=client_factory),
            StaticBaselineAdapter(
                StructuredSourceInfo(
                    id="owasp",
                    name="OWASP Security Baselines",
                    description=(
                        "Versioned OWASP Top 10:2021, ASVS 4.0.3, and WSTG 4.2 "
                        "documents retained from the original crawler"
                    ),
                    mode="bundled",
                    default_limit=3,
                ),
                OWASP_DOCUMENTS,
            ),
            StaticBaselineAdapter(
                StructuredSourceInfo(
                    id="nist",
                    name="NIST Baselines",
                    description="Versioned NIST CSF 2.0 and SP 800-53 Rev. 5 baselines",
                    mode="bundled",
                    default_limit=2,
                ),
                NIST_DOCUMENTS,
            ),
            StaticBaselineAdapter(
                StructuredSourceInfo(
                    id="china_standards",
                    name="中国法规与标准基线",
                    description="网络、数据、个人信息、AI 与云安全导航基线",
                    mode="bundled",
                    default_limit=5,
                ),
                CHINA_DOCUMENTS,
            ),
            LegacyCorpusAdapter(legacy_corpus_root),
        )
    )


class StructuredSourceRegistry:
    def __init__(self, adapters: tuple[StructuredSourceAdapter, ...]) -> None:
        self._adapters = {adapter.info.id: adapter for adapter in adapters}

    def infos(self) -> list[StructuredSourceInfo]:
        return [adapter.info for adapter in self._adapters.values()]

    def get(self, source_id: str) -> StructuredSourceAdapter:
        try:
            return self._adapters[source_id]
        except KeyError as error:
            raise ValueError(f"Unknown structured crawler source: {source_id}") from error
