"""Dual-index Experience projections into Qdrant + OpenSearch (+ MySQL chunk rows)."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding.client import EmbeddingClient
from app.core.embedding.profiles import get_embedding_profile, profile_settings
from app.core.indexing.opensearch_indexer import get_opensearch_indexer
from app.core.indexing.qdrant_indexer import get_qdrant_indexer
from app.domain import DocumentStatus
from app.stores.chunk_store import ChunkStore
from app.stores.db import get_engine
from app.stores.document_store import increment_content_revision
from app.stores.models import ChunkRow, DocumentRow, ExperienceItemRow

logger = logging.getLogger(__name__)


def experience_chunk_id(experience_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"trustguard:experience-chunk:{experience_id}"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _chunk_payload(row: ExperienceItemRow, *, embedding_model: str, embedding_dim: int) -> dict[str, Any]:
    chunk_id = experience_chunk_id(row.id)
    metadata = {
        "source_type": "experience",
        "experience_id": row.id,
        "external_id": row.external_id,
        "workflow_type": row.workflow_type,
        "knowledge_scope": row.knowledge_scope,
        "visibility": row.visibility,
        "workspace_id": row.workspace_id,
        "experience_type": row.experience_type,
        "status": row.status,
        "knowledge_base_id": row.knowledge_base_id,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }
    return {
        "id": chunk_id,
        "document_id": row.id,
        "knowledge_base_id": row.knowledge_base_id,
        "chunk_index": 0,
        "page_no": None,
        "text": row.search_text,
        "token_count": max(len(row.search_text.split()), 1),
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "qdrant_point_id": chunk_id,
        "metadata": metadata,
        "status": "active",
    }


class ExperienceIndexer:
    async def upsert_proven(self, row: ExperienceItemRow) -> None:
        """Synchronously dual-write indexes and MySQL document/chunk projection."""
        profile = get_embedding_profile("configured")
        settings = profile_settings(profile)
        embedder = EmbeddingClient(settings)
        vectors = await embedder.embed_texts([row.search_text])
        chunk = _chunk_payload(
            row,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )
        source_uri = f"experience://{row.source_system}/{row.external_id}"
        filename = f"experience-{row.external_id}.txt"
        content_hash = hashlib.sha256(row.search_text.encode("utf-8")).hexdigest()

        qdrant = get_qdrant_indexer(settings, profile=profile)
        opensearch = get_opensearch_indexer()
        try:
            await qdrant.upsert_chunks(
                document_id=row.id,
                chunks=[chunk],
                vectors=vectors,
                source_uri=source_uri,
                original_filename=filename,
            )
            await opensearch.ensure_index()
            await opensearch.index_chunks(
                [chunk],
                source_uri=source_uri,
                original_filename=filename,
            )
            await self._upsert_mysql_projection(
                row,
                chunk=chunk,
                source_uri=source_uri,
                filename=filename,
                content_hash=content_hash,
            )
        except Exception:
            logger.exception("experience dual index failed for %s", row.id)
            try:
                await self.delete_proven(row.id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "experience index compensation failed for %s",
                    row.id,
                    exc_info=True,
                )
            raise

    async def _upsert_mysql_projection(
        self,
        row: ExperienceItemRow,
        *,
        chunk: dict[str, Any],
        source_uri: str,
        filename: str,
        content_hash: str,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            existing = await session.get(DocumentRow, row.id)
            metadata = {
                "source_type": "experience",
                "experience_id": row.id,
                "workflow_type": row.workflow_type,
                "visibility": row.visibility,
                "workspace_id": row.workspace_id,
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            }
            if existing is None:
                session.add(
                    DocumentRow(
                        id=row.id,
                        knowledge_base_id=row.knowledge_base_id,
                        source_type="experience",
                        source_uri=source_uri,
                        content_hash=content_hash,
                        title=f"Experience {row.external_id}",
                        mime_type="text/plain",
                        original_filename=filename,
                        doc_version=row.source_revision,
                        status=DocumentStatus.READY,
                        blob_path=None,
                        metadata_json=metadata,
                    )
                )
            else:
                existing.knowledge_base_id = row.knowledge_base_id
                existing.source_type = "experience"
                existing.source_uri = source_uri
                existing.content_hash = content_hash
                existing.title = f"Experience {row.external_id}"
                existing.original_filename = filename
                existing.doc_version = row.source_revision
                existing.status = DocumentStatus.READY
                existing.metadata_json = metadata
                existing.updated_at = _utcnow()
            await session.execute(delete(ChunkRow).where(ChunkRow.document_id == row.id))
            session.add(ChunkStore._row_from_chunk(chunk))
            await increment_content_revision(session, row.knowledge_base_id)
            await session.commit()

    async def delete_proven(self, experience_id: str) -> bool:
        profile = get_embedding_profile("configured")
        settings = profile_settings(profile)
        qdrant = get_qdrant_indexer(settings, profile=profile)
        opensearch = get_opensearch_indexer()
        removed = True
        try:
            await qdrant.delete_document(experience_id)
        except Exception:  # noqa: BLE001
            removed = False
            logger.warning(
                "failed to delete experience qdrant points for %s",
                experience_id,
                exc_info=True,
            )
        try:
            await opensearch.delete_for_document(experience_id)
        except Exception:  # noqa: BLE001
            removed = False
            logger.warning(
                "failed to delete experience opensearch docs for %s",
                experience_id,
                exc_info=True,
            )
        async with AsyncSession(get_engine()) as session:
            await session.execute(delete(ChunkRow).where(ChunkRow.document_id == experience_id))
            doc = await session.get(DocumentRow, experience_id)
            if doc is not None:
                await increment_content_revision(session, doc.knowledge_base_id)
                await session.delete(doc)
            await session.commit()
        return removed


def get_experience_indexer() -> ExperienceIndexer:
    return ExperienceIndexer()
