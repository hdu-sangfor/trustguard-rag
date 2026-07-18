from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.health import _ingest_reported, _ingest_required
from app.settings import get_settings
from app.stores import opensearch_store


def test_ingest_health_requires_real_search_backends(monkeypatch) -> None:
    monkeypatch.setenv("RAG_QDRANT_MOCK", "false")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "false")
    monkeypatch.setenv("RAG_MINIO_ENABLED", "false")
    get_settings.cache_clear()

    assert _ingest_required() == (
        "mysql",
        "local_storage",
        "qdrant",
        "opensearch",
        "mineru",
    )
    assert _ingest_reported() == (*_ingest_required(), "rabbitmq")

    get_settings.cache_clear()


def test_ingest_health_reports_disabled_search_backends_once(monkeypatch) -> None:
    monkeypatch.setenv("RAG_QDRANT_MOCK", "true")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "true")
    monkeypatch.setenv("RAG_MINIO_ENABLED", "false")
    get_settings.cache_clear()

    assert _ingest_required() == ("mysql", "local_storage", "mineru")
    assert _ingest_reported() == (
        "mysql",
        "local_storage",
        "mineru",
        "qdrant",
        "opensearch",
        "rabbitmq",
    )

    get_settings.cache_clear()


def test_local_pdf_parser_still_requires_mineru_for_docx(monkeypatch) -> None:
    monkeypatch.setenv("RAG_PDF_PARSER", "local")
    monkeypatch.setenv("RAG_QDRANT_MOCK", "true")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "true")
    monkeypatch.setenv("RAG_MINIO_ENABLED", "false")
    get_settings.cache_clear()

    assert _ingest_required() == ("mysql", "local_storage", "mineru")
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_opensearch_health_is_disabled_in_mock_mode(monkeypatch) -> None:
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "true")
    get_settings.cache_clear()

    result = await opensearch_store.check()

    assert result.status == "disabled"
    assert result.detail == "opensearch mock mode (in-memory keyword index)"
    get_settings.cache_clear()


@pytest.mark.anyio
async def test_opensearch_health_requires_business_index(monkeypatch) -> None:
    client = SimpleNamespace(
        ping=AsyncMock(return_value=True),
        indices=SimpleNamespace(exists=AsyncMock(return_value=False)),
    )
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(opensearch_store, "get_client", lambda: client)

    result = await opensearch_store.check()

    assert result.status == "down"
    assert result.detail == "missing index: rag_chunks"
    get_settings.cache_clear()
