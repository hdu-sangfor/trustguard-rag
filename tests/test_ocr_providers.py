"""OCR provider and API review tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.ocr.custom_http_provider import CustomHttpOcrProvider, _resolve_jsonpath
from app.core.ocr.errors import OcrError
from app.core.ocr.factory import (
    OcrEngine,
    build_ocr_provider,
    normalize_ocr_provider,
    reset_ocr_engine_cache,
)
from app.core.ocr.none_provider import NoneOcrProvider
from app.core.ocr.openai_compatible_provider import OpenAICompatibleOcrProvider
from app.core.ocr.protocol import OcrRecognizeResult
from app.settings import get_settings


class FakeOcr:
    name = "fake"

    def __init__(self, text: str = "hello ocr", empty: bool = False) -> None:
        self.text = text
        self.empty = empty

    async def recognize(self, image_bytes: bytes, *, lang: str | None = None) -> OcrRecognizeResult:
        return OcrRecognizeResult(text="" if self.empty else self.text, confidence=0.9, empty=self.empty)


@pytest.fixture(autouse=True)
def _clear_ocr(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "none")
    get_settings.cache_clear()
    reset_ocr_engine_cache()
    yield
    get_settings.cache_clear()
    reset_ocr_engine_cache()


def test_normalize_ocr_provider():
    assert normalize_ocr_provider("local") == "local"
    assert normalize_ocr_provider("paddle") == "local"
    assert normalize_ocr_provider("api") == "api"
    assert normalize_ocr_provider("none") == "none"


def test_build_none_provider():
    provider = build_ocr_provider(get_settings())
    assert isinstance(provider, NoneOcrProvider)


@pytest.mark.asyncio
async def test_ocr_engine_fail_open(monkeypatch):
    monkeypatch.setenv("RAG_OCR_PROVIDER", "api")
    monkeypatch.setenv("RAG_OCR_FAIL_OPEN", "true")
    get_settings.cache_clear()

    class Boom:
        name = "boom"

        async def recognize(self, image_bytes, *, lang=None):
            raise OcrError("boom")

    engine = OcrEngine(provider=Boom(), settings=get_settings())
    result = await engine.recognize(b"img")
    assert result.empty is True
    assert result.text == ""


@pytest.mark.asyncio
async def test_ocr_engine_recognize_region_failed_status(monkeypatch):
    monkeypatch.setenv("RAG_OCR_FAIL_OPEN", "true")
    get_settings.cache_clear()

    class Boom:
        name = "boom"

        async def recognize(self, image_bytes, *, lang=None):
            raise OcrError("boom")

    engine = OcrEngine(provider=Boom(), settings=get_settings())
    draft = await engine.recognize_region(page_no=1, bbox=[0, 0, 10, 10], crop_png=b"png")
    assert draft.status == "failed"
    assert draft.error_message


def test_resolve_jsonpath():
    assert _resolve_jsonpath({"data": {"text": "abc"}}, "$.data.text") == "abc"
    with pytest.raises(OcrError):
        _resolve_jsonpath({"data": {}}, "$.data.text")


@pytest.mark.asyncio
async def test_openai_compatible_ocr_success(monkeypatch):
    monkeypatch.setenv("RAG_OCR_API_BASE_URL", "https://1.1.1.1/v1")
    monkeypatch.setenv("RAG_OCR_API_KEY", "sk-test")
    monkeypatch.setenv("RAG_OCR_API_MODEL", "vision-x")
    get_settings.cache_clear()
    provider = OpenAICompatibleOcrProvider(get_settings())

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "API OCR TEXT"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(
        "app.core.ocr.openai_compatible_provider.httpx.AsyncClient", lambda **kw: Client()
    )
    result = await provider.recognize(b"fake-image")
    assert result.text == "API OCR TEXT"
    assert result.empty is False


@pytest.mark.asyncio
async def test_openai_compatible_ocr_4xx(monkeypatch):
    monkeypatch.setenv("RAG_OCR_API_BASE_URL", "https://1.1.1.1/v1")
    monkeypatch.setenv("RAG_OCR_API_KEY", "sk-test")
    get_settings.cache_clear()
    provider = OpenAICompatibleOcrProvider(get_settings())

    class Resp:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "bad",
                request=httpx.Request("POST", "https://1.1.1.1/v1"),
                response=httpx.Response(400),
            )

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr(
        "app.core.ocr.openai_compatible_provider.httpx.AsyncClient", lambda **kw: Client()
    )
    with pytest.raises(OcrError):
        await provider.recognize(b"img")


@pytest.mark.asyncio
async def test_openai_compatible_ocr_timeout(monkeypatch):
    monkeypatch.setenv("RAG_OCR_API_BASE_URL", "https://1.1.1.1/v1")
    monkeypatch.setenv("RAG_OCR_API_KEY", "sk-test")
    get_settings.cache_clear()
    provider = OpenAICompatibleOcrProvider(get_settings())

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(
        "app.core.ocr.openai_compatible_provider.httpx.AsyncClient", lambda **kw: Client()
    )
    with pytest.raises(OcrError):
        await provider.recognize(b"img")


@pytest.mark.asyncio
async def test_custom_http_ocr_multipart(monkeypatch):
    monkeypatch.setenv("RAG_OCR_CUSTOM_BASE_URL", "https://8.8.8.8")
    monkeypatch.setenv("RAG_OCR_CUSTOM_PATH", "/ocr")
    monkeypatch.setenv("RAG_OCR_CUSTOM_REQUEST_TEMPLATE", "multipart")
    monkeypatch.setenv("RAG_OCR_CUSTOM_RESPONSE_JSONPATH", "$.text")
    get_settings.cache_clear()
    provider = CustomHttpOcrProvider(get_settings())
    seen: dict = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"text": "multipart ok"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            seen["files"] = kwargs.get("files")
            seen["data"] = kwargs.get("data")
            return Resp()

    monkeypatch.setattr("app.core.ocr.custom_http_provider.httpx.AsyncClient", lambda **kw: Client())
    result = await provider.recognize(b"png-bytes")
    assert result.text == "multipart ok"
    assert seen.get("files") is not None


@pytest.mark.asyncio
async def test_custom_http_ocr_base64(monkeypatch):
    monkeypatch.setenv("RAG_OCR_CUSTOM_BASE_URL", "https://8.8.8.8")
    monkeypatch.setenv("RAG_OCR_CUSTOM_PATH", "/v1/ocr")
    monkeypatch.setenv("RAG_OCR_CUSTOM_REQUEST_TEMPLATE", "base64_json")
    monkeypatch.setenv("RAG_OCR_CUSTOM_RESPONSE_JSONPATH", "$.result.text")
    monkeypatch.setenv("RAG_OCR_CUSTOM_API_KEY", "k")
    get_settings.cache_clear()
    provider = CustomHttpOcrProvider(get_settings())

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"text": "custom ok"}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return Resp()

    monkeypatch.setattr("app.core.ocr.custom_http_provider.httpx.AsyncClient", lambda **kw: Client())
    result = await provider.recognize(b"x")
    assert result.text == "custom ok"


def test_ocr_url_blocks_private_by_default():
    from app.core.ocr.url_safety import assert_safe_ocr_url

    with pytest.raises(OcrError):
        assert_safe_ocr_url("http://127.0.0.1:8080/ocr", allow_private=False)
    with pytest.raises(OcrError):
        assert_safe_ocr_url("http://10.0.0.5/ocr", allow_private=False)
    assert_safe_ocr_url("http://127.0.0.1:8080/ocr", allow_private=True)


def test_ocr_url_blocks_hostname_resolving_private(monkeypatch):
    from app.core.ocr import url_safety

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(None, None, None, None, ("10.1.2.3", port))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(OcrError):
        url_safety.assert_safe_ocr_url("https://evil.internal/ocr", allow_private=False)


@pytest.mark.asyncio
async def test_custom_ocr_rejects_private_url(monkeypatch):
    monkeypatch.setenv("RAG_OCR_CUSTOM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("RAG_OCR_ALLOW_PRIVATE_URLS", "false")
    get_settings.cache_clear()
    provider = CustomHttpOcrProvider(get_settings())
    with pytest.raises(OcrError):
        await provider.recognize(b"x")


@pytest.mark.asyncio
async def test_ocr_review_api_approve_and_correct(client, test_engine, tmp_storage, monkeypatch):
    from app.core.ocr.protocol import OcrRegionDraft
    from app.domain import DocumentStatus, OcrRegionStatus
    from app.stores.blob_store import get_blob_store
    from app.stores.document_store import DocumentStore
    from app.stores.ocr_region_store import OcrRegionStore

    docs = DocumentStore()
    doc = await docs.create(
        source_type="file",
        source_uri="upload://ocr-doc",
        content_hash="abc",
        status=DocumentStatus.READY,
        mime_type="application/pdf",
        original_filename="a.pdf",
        document_id="11111111-1111-1111-1111-111111111111",
    )
    blobs = get_blob_store()
    blobs.commit_bundle(
        doc.id,
        raw_name="raw.pdf",
        raw_bytes=b"%PDF-1.4",
        extracted_text="base text",
        meta={},
    )
    store = OcrRegionStore(blob_store=blobs)
    await store.create_from_drafts(
        doc.id,
        [
            OcrRegionDraft(
                page_no=1,
                bbox=[0, 0, 1, 1],
                crop_png=b"\x89PNG\r\n\x1a\nocr",
                ocr_text="wrong text",
                status="pending",
                provider="fake",
            )
        ],
    )
    regions = await store.list_for_document(doc.id)
    rid = regions[0].id

    listed = await client.get(f"/v1/documents/{doc.id}/ocr-regions")
    assert listed.status_code == 200
    assert listed.json()[0]["ocr_text"] == "wrong text"

    img = await client.get(f"/v1/ocr-regions/{rid}/image")
    assert img.status_code == 200
    assert img.content.startswith(b"\x89PNG")

    approved = await client.post(f"/v1/ocr-regions/{rid}/review", json={"action": "approve"})
    assert approved.status_code == 200
    assert approved.json()["status"] == OcrRegionStatus.APPROVED.value

    # 幂等：再次 approve
    approved2 = await client.post(f"/v1/ocr-regions/{rid}/review", json={"action": "approve"})
    assert approved2.status_code == 200
    assert approved2.json()["status"] == OcrRegionStatus.APPROVED.value

    monkeypatch.setattr(
        "app.api.ocr_review.get_ingest_pipeline",
        lambda: SimpleNamespace(republish_from_ocr_corrections=AsyncMock()),
    )
    corrected = await client.post(
        f"/v1/ocr-regions/{rid}/review",
        json={"action": "correct", "corrected_text": "fixed text"},
    )
    assert corrected.status_code == 200
    assert corrected.json()["status"] == OcrRegionStatus.CORRECTED.value
    assert corrected.json()["corrected_text"] == "fixed text"


@pytest.mark.asyncio
async def test_ocr_approve_does_not_reindex(client, test_engine, tmp_storage, monkeypatch):
    from app.core.ocr.protocol import OcrRegionDraft
    from app.domain import DocumentStatus
    from app.stores.blob_store import get_blob_store
    from app.stores.chunk_store import ChunkStore
    from app.stores.document_store import DocumentStore
    from app.stores.ocr_region_store import OcrRegionStore

    docs = DocumentStore()
    doc = await docs.create(
        source_type="file",
        source_uri="upload://ocr-approve",
        content_hash="approve-hash",
        status=DocumentStatus.READY,
        mime_type="image/png",
        original_filename="a.png",
    )
    blobs = get_blob_store()
    blobs.commit_bundle(
        doc.id,
        raw_name="raw.png",
        raw_bytes=b"\x89PNG\r\n\x1a\n",
        extracted_text="unchanged base",
        meta={},
    )
    store = OcrRegionStore(blob_store=blobs)
    await store.create_from_drafts(
        doc.id,
        [
            OcrRegionDraft(
                page_no=1,
                bbox=[0, 0, 1, 1],
                crop_png=b"\x89PNG\r\n\x1a\ncrop",
                ocr_text="guess",
                status="pending",
                provider="fake",
            )
        ],
    )
    rid = (await store.list_for_document(doc.id))[0].id

    republish = AsyncMock()
    monkeypatch.setattr(
        "app.api.ocr_review.get_ingest_pipeline",
        lambda: SimpleNamespace(republish_from_ocr_corrections=republish),
    )
    before = await ChunkStore().list_for_document(doc.id)
    resp = await client.post(f"/v1/ocr-regions/{rid}/review", json={"action": "approve"})
    assert resp.status_code == 200
    republish.assert_not_called()
    after = await ChunkStore().list_for_document(doc.id)
    assert len(before) == len(after)


@pytest.mark.asyncio
async def test_ocr_correct_reindexes_chunks(client, test_engine, tmp_storage, monkeypatch):
    """correct 后 republish：分块文本应包含人工纠正内容。"""
    from app.core.ocr.protocol import OcrRegionDraft
    from app.domain import DocumentStatus, OcrRegionStatus
    from app.stores.blob_store import get_blob_store
    from app.stores.chunk_store import ChunkStore
    from app.stores.document_store import DocumentStore
    from app.stores.ocr_region_store import OcrRegionStore

    monkeypatch.setenv("RAG_QDRANT_MOCK", "true")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "true")
    get_settings.cache_clear()

    docs = DocumentStore()
    doc = await docs.create(
        source_type="file",
        source_uri="upload://ocr-reindex",
        content_hash="reindex-hash",
        status=DocumentStatus.READY,
        mime_type="image/png",
        original_filename="scan.png",
    )
    blobs = get_blob_store()
    blobs.commit_bundle(
        doc.id,
        raw_name="raw.png",
        raw_bytes=b"\x89PNG\r\n\x1a\n",
        extracted_text="base layer",
        meta={},
    )
    store = OcrRegionStore(blob_store=blobs)
    await store.create_from_drafts(
        doc.id,
        [
            OcrRegionDraft(
                page_no=1,
                bbox=[0, 0, 10, 10],
                crop_png=b"\x89PNG\r\n\x1a\ncrop",
                ocr_text="machine guess",
                status="pending",
                provider="fake",
            )
        ],
    )
    rid = (await store.list_for_document(doc.id))[0].id

    resp = await client.post(
        f"/v1/ocr-regions/{rid}/review",
        json={"action": "correct", "corrected_text": "HUMAN_FIXED_OCR"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == OcrRegionStatus.CORRECTED.value

    chunks = await ChunkStore().list_for_document(doc.id)
    joined = "\n".join(c.text for c in chunks)
    assert "HUMAN_FIXED_OCR" in joined
