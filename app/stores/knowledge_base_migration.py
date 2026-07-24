"""知识库增量表结构与历史文档归属迁移。"""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from qdrant_client.models import FieldCondition, Filter, MatchValue, PayloadSchemaType
from sqlalchemy import inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding.profiles import (
    canonical_embedding_profile_id,
    collection_name,
    list_embedding_profiles,
    profile_settings,
)
from app.settings import get_settings
from app.core.retrieval.security_entities import build_security_entity_fields
from app.stores import qdrant_store
from app.stores.db import get_engine
from app.stores.knowledge_base_store import KnowledgeBaseStore
from app.stores.models import Base, ChunkRow, DocumentRow

logger = logging.getLogger(__name__)


async def ensure_knowledge_base_schema() -> None:
    """为 create_all 无法修改的既有 documents 表补充知识库字段。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        columns = await conn.run_sync(
            lambda sync_conn: {
                item["name"] for item in inspect(sync_conn).get_columns("documents")
            }
        )
        if "knowledge_base_id" not in columns:
            await conn.execute(
                text("ALTER TABLE documents ADD COLUMN knowledge_base_id VARCHAR(36) NULL")
            )
        kb_columns = await conn.run_sync(
            lambda sync_conn: {
                item["name"]
                for item in inspect(sync_conn).get_columns("knowledge_bases")
            }
        )
        if "embedding_api_driver" not in kb_columns:
            await conn.execute(
                text(
                    "ALTER TABLE knowledge_bases ADD COLUMN embedding_api_driver "
                    "VARCHAR(32) NOT NULL DEFAULT 'openai_compatible'"
                )
            )
        await conn.execute(
            text(
                "UPDATE knowledge_bases SET embedding_provider = 'api', "
                "embedding_api_driver = 'bailian' "
                "WHERE embedding_provider IN ('bailian', 'dashscope', 'aliyun')"
            )
        )
        indexes = await conn.run_sync(
            lambda sync_conn: {
                item["name"] for item in inspect(sync_conn).get_indexes("documents")
            }
        )
        if "idx_documents_knowledge_base" not in indexes:
            await conn.execute(
                text(
                    "CREATE INDEX idx_documents_knowledge_base "
                    "ON documents (knowledge_base_id)"
                )
            )
        if conn.dialect.name == "mysql" and "uq_document_source" in indexes:
            await conn.execute(text("ALTER TABLE documents DROP INDEX uq_document_source"))
        if conn.dialect.name == "mysql" and "uq_document_kb_source" not in indexes:
            await conn.execute(
                text(
                    "ALTER TABLE documents ADD UNIQUE KEY uq_document_kb_source "
                    "(knowledge_base_id, source_type, source_uri(256), content_hash)"
                )
            )


async def migrate_legacy_knowledge_bases() -> int:
    """按历史 embedding profile 分组旧文档并回填知识库归属。"""
    profiles = {profile.id: profile for profile in list_embedding_profiles()}
    store = KnowledgeBaseStore()
    default_kb = await store.get_default()

    async with AsyncSession(get_engine()) as session:
        documents = list(
            (
                await session.execute(
                    select(DocumentRow).where(DocumentRow.knowledge_base_id.is_(None))
                )
            )
            .scalars()
            .all()
        )
        if not documents:
            return 0
        document_ids = [doc.id for doc in documents]
        rows = (
            await session.execute(
                select(ChunkRow.document_id, ChunkRow.metadata_json)
                .where(ChunkRow.document_id.in_(document_ids))
                .order_by(ChunkRow.document_id, ChunkRow.chunk_index)
            )
        ).all()

    profile_by_document: dict[str, str] = {}
    for document_id, metadata in rows:
        profile_by_document.setdefault(
            document_id,
            canonical_embedding_profile_id(
                (metadata or {}).get("embedding_profile", "configured")
            ),
        )

    knowledge_base_by_profile = {"configured": default_kb}
    for profile_id in sorted(set(profile_by_document.values()) - {"configured"}):
        profile = profiles.get(profile_id)
        if profile is None:
            logger.warning("unknown historical embedding profile %s; using default", profile_id)
            continue
        knowledge_base_id = uuid5(
            NAMESPACE_URL,
            f"trustguard:knowledge-base:legacy:{profile_id}",
        )
        existing = await store.get(str(knowledge_base_id))
        if existing is None:
            try:
                existing = await store.create(
                    name=f"历史知识库 · {profile.label}"[:128],
                    description="由系统按历史向量化模型自动迁移。",
                    profile=profile,
                    knowledge_base_id=str(knowledge_base_id),
                    is_system=True,
                )
            except ValueError:
                existing = await store.get(str(knowledge_base_id))
        if existing:
            knowledge_base_by_profile[profile_id] = existing

    async with AsyncSession(get_engine()) as session:
        for doc in documents:
            profile_id = profile_by_document.get(doc.id, "configured")
            kb = knowledge_base_by_profile.get(profile_id, default_kb)
            await session.execute(
                update(DocumentRow)
                .where(DocumentRow.id == doc.id)
                .values(knowledge_base_id=kb.id)
            )
            chunk_rows = (
                (
                    await session.execute(
                        select(ChunkRow).where(ChunkRow.document_id == doc.id)
                    )
                )
                .scalars()
                .all()
            )
            for chunk in chunk_rows:
                chunk.metadata_json = {
                    **(chunk.metadata_json or {}),
                    "knowledge_base_id": kb.id,
                }
        await session.commit()

    return len(documents)


async def backfill_qdrant_knowledge_base_payloads() -> int:
    """幂等回填全部历史向量点，失败后可在下次启动继续重试。"""
    settings = get_settings()
    if settings.qdrant_mock:
        return 0
    async with AsyncSession(get_engine()) as session:
        documents = list((await session.execute(select(DocumentRow))).scalars().all())
        chunks = list(
            (
                await session.execute(
                    select(ChunkRow).order_by(
                        ChunkRow.document_id, ChunkRow.chunk_index
                    )
                )
            )
            .scalars()
            .all()
        )
    profile_by_document: dict[str, str] = {}
    chunks_by_document: dict[str, list[ChunkRow]] = defaultdict(list)
    for chunk in chunks:
        document_id = chunk.document_id
        metadata = chunk.metadata_json
        chunks_by_document[document_id].append(chunk)
        profile_by_document.setdefault(
            document_id,
            canonical_embedding_profile_id(
                (metadata or {}).get("embedding_profile", "configured")
            ),
        )
    assignment = {
        doc.id: (
            doc.knowledge_base_id,
            profile_by_document.get(doc.id, "configured"),
        )
        for doc in documents
        if doc.knowledge_base_id
    }
    security_fields_by_document = {
        doc.id: build_security_entity_fields(
            text="\n".join(
                chunk.text for chunk in chunks_by_document.get(doc.id, [])
            ),
            original_filename=doc.original_filename,
            metadata=(
                chunks_by_document[doc.id][0].metadata_json
                if chunks_by_document.get(doc.id)
                else {}
            ),
        )
        for doc in documents
    }
    await _backfill_qdrant_payloads(
        assignment,
        {profile.id: profile for profile in list_embedding_profiles()},
        security_fields_by_document,
    )
    return len(assignment)


async def _backfill_qdrant_payloads(
    assignment: dict[str, tuple[str, str]],
    profiles: dict,
    security_fields_by_document: dict[str, dict] | None = None,
) -> None:
    settings = get_settings()
    if settings.qdrant_mock or not assignment:
        return
    grouped: dict[tuple[str, str], list[str]] = defaultdict(list)
    for document_id, (knowledge_base_id, profile_id) in assignment.items():
        grouped[(profile_id, knowledge_base_id)].append(document_id)
    client = qdrant_store.get_client()
    collections = {item.name for item in (await client.get_collections()).collections}
    for (profile_id, knowledge_base_id), document_ids in grouped.items():
        profile = profiles.get(profile_id) or profiles["configured"]
        name = collection_name(profile, profile_settings(profile, settings))
        if name not in collections:
            continue
        await client.create_payload_index(
            collection_name=name,
            field_name="knowledge_base_id",
            field_schema=PayloadSchemaType.KEYWORD,
            wait=True,
        )
        for field_name in (
            "entity_id",
            "entity_type",
            "entity_ids",
            "entity_types",
            "aliases",
        ):
            await client.create_payload_index(
                collection_name=name,
                field_name=field_name,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
        for document_id in document_ids:
            security_fields = (security_fields_by_document or {}).get(
                document_id, {}
            )
            await client.set_payload(
                collection_name=name,
                payload={
                    "knowledge_base_id": knowledge_base_id,
                    **security_fields,
                },
                points=Filter(
                    must=[
                        FieldCondition(
                            key="document_id", match=MatchValue(value=document_id)
                        )
                    ]
                ),
            )
