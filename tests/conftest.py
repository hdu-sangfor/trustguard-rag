"""共享的 pytest 测试夹具。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.main import create_app
from app.settings import get_settings
from app.stores import db
from app.stores.models import Base


@pytest.fixture
def tmp_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setenv("RAG_LOCAL_STORAGE_DIR", str(storage))
    monkeypatch.setenv("RAG_MODE", "ingest")
    monkeypatch.setenv("RAG_QDRANT_MOCK", "true")
    monkeypatch.setenv("RAG_MINIO_ENABLED", "false")
    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "pseudo")
    monkeypatch.setenv("RAG_WORKER_EAGER", "true")
    get_settings.cache_clear()
    return storage


@pytest.fixture
async def test_engine(tmp_storage: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    db_path = tmp_storage / "test.db"
    dsn = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("RAG_MYSQL_HOST", "unused")
    get_settings.cache_clear()

    engine = create_async_engine(dsn, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    db._engine = engine  # type: ignore[attr-defined]

    def _get() -> AsyncEngine:
        return engine

    monkeypatch.setattr(db, "get_engine", _get)
    yield engine
    await engine.dispose()
    db._engine = None  # type: ignore[attr-defined]
    get_settings.cache_clear()


@pytest.fixture
def mock_qdrant(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.get_collections = AsyncMock(
        return_value=MagicMock(collections=[MagicMock(name="rag_chunks")])
    )
    client.create_collection = AsyncMock()
    client.create_payload_index = AsyncMock()
    client.upsert = AsyncMock()
    client.delete = AsyncMock()
    monkeypatch.setattr("app.stores.qdrant_store.get_client", lambda: client)
    return client


@pytest.fixture
async def client(
    test_engine: AsyncEngine, tmp_storage: Path, mock_qdrant: MagicMock
) -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

