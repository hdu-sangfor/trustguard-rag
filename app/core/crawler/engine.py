"""无全局状态的异步网页采集引擎。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
import trafilatura

from app.core.crawler.safety import UnsafeUrlError, validate_public_url
from app.core.crawler.transport import SafeAsyncHTTPTransport

_SPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MAX_RETRY_DELAY_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 5_000_000


@dataclass(slots=True)
class CrawlRequest:
    urls: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    site_urls: list[str] = field(default_factory=list)
    structured_sources: list[str] = field(default_factory=list)
    source_options: dict[str, dict[str, object]] = field(default_factory=dict)
    max_results_per_keyword: int = 10
    max_pages_per_site: int = 10
    max_total_pages: int = 100
    max_chars: int = 0
    timeout_seconds: float = 25.0
    fetch_delay_seconds: float = 1.0
    max_retries: int = 2
    retry_base_seconds: float = 1.0
    force: bool = False
    allow_private_urls: bool = False
    user_agent: str = "TrustGuardCrawler/1.0 (+https://github.com/hdu-sangfor/trustguard-rag)"


@dataclass(slots=True)
class CrawlPage:
    url: str
    title: str
    markdown: str
    content_hash: str
    source_type: str = "url"
    metadata: dict[str, object] = field(default_factory=dict)


def normalize_url(url: str) -> str:
    """移除 fragment 与常见追踪参数，生成稳定 URL 去重键。"""
    parsed = urlsplit((url or "").strip())
    query_parts: list[str] = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0].lower()
        if key in _TRACKING_QUERY_KEYS or key.startswith(_TRACKING_QUERY_PREFIXES):
            continue
        query_parts.append(part)
    path = parsed.path or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            (parsed.netloc or "").lower(),
            path,
            "&".join(query_parts),
            "",
        )
    )


def _clean_text(text: str) -> str:
    lines = [_SPACE.sub(" ", line).strip() for line in (text or "").splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def extract_page(html: str, url: str, *, max_chars: int = 0) -> CrawlPage:
    """从 HTML 提取正文并生成稳定 Markdown。"""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = _clean_text(soup.title.string)
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_images=False,
        favor_precision=True,
    )
    if not extracted:
        main = soup.find("main") or soup.find("article") or soup.body or soup
        extracted = main.get_text("\n", strip=True)
    body = _clean_text(extracted)
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n\n…[truncated]"
    if not body:
        raise ValueError("No readable page content was extracted")
    if not title:
        title = urlsplit(url).hostname or url
    markdown = f"# {title}\n\n> 来源：{url}\n\n{body}".strip()
    return CrawlPage(
        url=normalize_url(url),
        title=title[:512],
        markdown=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        metadata={"crawler_parser": "trafilatura", "source_url": normalize_url(url)},
    )


class CrawlEngine:
    """组合搜索、站点发现、URL 安全校验与正文提取。"""

    def __init__(
        self,
        *,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        validator: Callable[..., Awaitable[str]] = validate_public_url,
        searcher: Callable[[str, int], Awaitable[list[str]]] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._validator = validator
        self._searcher = searcher or self._search_duckduckgo

    def _client(
        self,
        request: CrawlRequest,
        **kwargs,
    ) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(**kwargs)
        limits = kwargs.pop("limits", httpx.Limits())
        return httpx.AsyncClient(
            **kwargs,
            trust_env=False,
            transport=SafeAsyncHTTPTransport(
                allow_private=request.allow_private_urls,
                limits=limits,
            ),
        )

    async def crawl(
        self,
        request: CrawlRequest,
        *,
        should_skip: Callable[[str], Awaitable[bool]] | None = None,
        control: Callable[[], Awaitable[str | None]] | None = None,
        on_error: Callable[[str, Exception], Awaitable[None]] | None = None,
    ) -> AsyncIterator[CrawlPage]:
        candidates = await self._collect_candidates(request, on_error=on_error)
        seen: set[str] = set()
        emitted = 0
        network_attempts = 0
        limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)
        headers = {
            "User-Agent": request.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        async with self._client(
            request,
            timeout=request.timeout_seconds,
            follow_redirects=False,
            limits=limits,
            headers=headers,
        ) as client:
            for raw_url in candidates:
                if (
                    emitted >= request.max_total_pages
                    or network_attempts >= request.max_total_pages
                ):
                    break
                if control and await control() in {"pause", "cancel", "lost"}:
                    break
                normalized = normalize_url(raw_url)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                if should_skip and not request.force and await should_skip(normalized):
                    continue
                network_attempts += 1
                try:
                    page = await self._fetch_page(client, normalized, request)
                except (httpx.HTTPError, UnsafeUrlError, ValueError) as error:
                    if on_error:
                        await on_error(normalized, error)
                    continue
                if control and await control() in {"pause", "cancel", "lost"}:
                    break
                emitted += 1
                yield page
                if request.fetch_delay_seconds > 0:
                    await asyncio.sleep(request.fetch_delay_seconds)

    async def _collect_candidates(
        self,
        request: CrawlRequest,
        *,
        on_error: Callable[[str, Exception], Awaitable[None]] | None = None,
    ) -> list[str]:
        candidates = [*request.urls]
        for keyword in request.keywords:
            try:
                candidates.extend(await self._search_with_retry(keyword, request))
            except Exception as error:
                if on_error:
                    await on_error(f"search:{keyword}", error)
        for site_url in request.site_urls[: request.max_total_pages]:
            try:
                candidates.extend(
                    await self._discover_site_links(
                        site_url,
                        limit=request.max_pages_per_site,
                        request=request,
                    )
                )
            except Exception as error:
                if on_error:
                    await on_error(site_url, error)
        return candidates

    async def _search_with_retry(
        self, keyword: str, request: CrawlRequest
    ) -> list[str]:
        for attempt in range(request.max_retries + 1):
            try:
                return await self._searcher(keyword, request.max_results_per_keyword)
            except Exception:
                if attempt >= request.max_retries:
                    raise
                await asyncio.sleep(self._retry_delay(request, attempt))
        return []

    async def _discover_site_links(
        self, site_url: str, *, limit: int, request: CrawlRequest
    ) -> list[str]:
        current = normalize_url(site_url)
        headers = {"User-Agent": request.user_agent}
        async with self._client(
            request,
            timeout=request.timeout_seconds,
            follow_redirects=False,
            headers=headers,
        ) as client:
            for _ in range(6):
                await self._validator(
                    current,
                    allow_private=request.allow_private_urls,
                )
                response = await self._request_with_retry(client, current, request)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect response is missing Location")
                    current = normalize_url(urljoin(current, location))
                    continue
                response.raise_for_status()
                if len(response.content) > _MAX_RESPONSE_BYTES:
                    raise ValueError("Site index response exceeds 5 MB")
                break
            else:
                raise ValueError("Too many redirects")
        base_host = urlsplit(current).hostname
        soup = BeautifulSoup(response.text, "html.parser")
        links = [current]
        for anchor in soup.find_all("a", href=True):
            candidate = normalize_url(urljoin(current, str(anchor["href"])))
            parsed = urlsplit(candidate)
            if parsed.scheme in {"http", "https"} and parsed.hostname == base_host:
                links.append(candidate)
            if len(dict.fromkeys(links)) >= limit:
                break
        return list(dict.fromkeys(links))[:limit]

    async def _fetch_page(
        self, client: httpx.AsyncClient, url: str, request: CrawlRequest
    ) -> CrawlPage:
        current = url
        for _ in range(6):
            await self._validator(current, allow_private=request.allow_private_urls)
            response = await self._request_with_retry(client, current, request)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect response is missing Location")
                current = normalize_url(urljoin(current, location))
                continue
            response.raise_for_status()
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise ValueError("Page response exceeds 5 MB")
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not any(
                value in content_type for value in ("text/html", "application/xhtml+xml")
            ):
                raise ValueError(f"Unsupported page content type: {content_type}")
            return extract_page(response.text, current, max_chars=request.max_chars)
        raise ValueError("Too many redirects")

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        request: CrawlRequest,
    ) -> httpx.Response:
        for attempt in range(request.max_retries + 1):
            try:
                response = await self._get_limited_response(client, url)
            except httpx.TransportError:
                if attempt >= request.max_retries:
                    raise
                await asyncio.sleep(self._retry_delay(request, attempt))
                continue
            if (
                response.status_code not in _RETRYABLE_STATUS_CODES
                or attempt >= request.max_retries
            ):
                return response
            await asyncio.sleep(self._retry_delay(request, attempt, response=response))
        raise RuntimeError("Crawler retry loop exited unexpectedly")

    @staticmethod
    async def _get_limited_response(
        client: httpx.AsyncClient,
        url: str,
    ) -> httpx.Response:
        """Read at most the configured page limit instead of buffering blindly."""
        stream = getattr(client, "stream", None)
        if stream is None:  # lightweight test clients
            response = await client.get(url)
            if len(response.content) > _MAX_RESPONSE_BYTES:
                raise ValueError("Page response exceeds 5 MB")
            return response

        async with stream("GET", url) as response:
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    pass
                else:
                    if declared_length > _MAX_RESPONSE_BYTES:
                        raise ValueError("Page response exceeds 5 MB")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise ValueError("Page response exceeds 5 MB")
            try:
                request = response.request
            except RuntimeError:
                request = httpx.Request("GET", url)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(content),
                request=request,
                extensions=response.extensions,
            )

    @staticmethod
    def _retry_delay(
        request: CrawlRequest,
        attempt: int,
        *,
        response: httpx.Response | None = None,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return min(max(float(retry_after), 0.0), _MAX_RETRY_DELAY_SECONDS)
                except ValueError:
                    pass
        return min(
            max(request.retry_base_seconds, 0.0) * (2**attempt),
            _MAX_RETRY_DELAY_SECONDS,
        )

    @staticmethod
    async def _search_duckduckgo(keyword: str, limit: int) -> list[str]:
        def run() -> list[str]:
            try:
                from ddgs import DDGS
            except ImportError as error:
                raise RuntimeError(
                    "Keyword search requires the optional ddgs dependency"
                ) from error
            rows = DDGS().text(keyword, max_results=limit)
            return [
                str(row.get("href") or row.get("url") or "")
                for row in rows
                if row.get("href") or row.get("url")
            ]

        return await asyncio.to_thread(run)
