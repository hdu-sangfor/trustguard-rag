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
from app.stores.models import (
    Base,
    ChunkRow,
    DocumentRow,
    IngestJobRow,
    KnowledgeBaseRow,
)

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
        job_columns = await conn.run_sync(
            lambda sync_conn: {
                item["name"] for item in inspect(sync_conn).get_columns("ingest_jobs")
            }
        )
        if "knowledge_base_id" not in job_columns:
            await conn.execute(
                text(
                    "ALTER TABLE ingest_jobs "
                    "ADD COLUMN knowledge_base_id VARCHAR(36) NULL"
                )
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
        job_indexes = await conn.run_sync(
            lambda sync_conn: {
                item["name"] for item in inspect(sync_conn).get_indexes("ingest_jobs")
            }
        )
        if "idx_jobs_knowledge_base_status" not in job_indexes:
            await conn.execute(
                text(
                    "CREATE INDEX idx_jobs_knowledge_base_status "
                    "ON ingest_jobs (knowledge_base_id, status)"
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


async def migrate_legacy_job_knowledge_bases() -> int:
    """把历史任务中的知识库快照回填到显式列，便于并发删除检查。"""
    store = KnowledgeBaseStore()
    default_kb = await store.get_default()
    async with AsyncSession(get_engine()) as session:
        jobs = list(
            (
                await session.execute(
                    select(IngestJobRow).where(
                        IngestJobRow.knowledge_base_id.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if not jobs:
            return 0
        requested_ids = {
            str((job.options_json or {}).get("knowledge_base_id"))
            for job in jobs
            if (job.options_json or {}).get("knowledge_base_id")
        }
        valid_ids = (
            set(
                (
                    await session.execute(
                        select(KnowledgeBaseRow.id).where(
                            KnowledgeBaseRow.id.in_(requested_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            if requested_ids
            else set()
        )
        for job in jobs:
            requested = (job.options_json or {}).get("knowledge_base_id")
            job.knowledge_base_id = (
                str(requested) if requested and str(requested) in valid_ids else default_kb.id
            )
        await session.commit()
    return len(jobs)


async def enforce_knowledge_base_integrity() -> None:
    """在回填完成后验证归属，并为 MySQL 收紧非空与外键约束。"""
    engine = get_engine()
    async with engine.begin() as conn:
        null_documents = int(
            (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM documents "
                        "WHERE knowledge_base_id IS NULL"
                    )
                )
            ).scalar_one()
        )
        orphan_documents = int(
            (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM documents d "
                        "LEFT JOIN knowledge_bases kb "
                        "ON kb.id = d.knowledge_base_id "
                        "WHERE kb.id IS NULL"
                    )
                )
            ).scalar_one()
        )
        if null_documents or orphan_documents:
            raise RuntimeError(
                "Knowledge base migration incomplete: "
                f"null_documents={null_documents}, "
                f"orphan_documents={orphan_documents}"
            )
        if conn.dialect.name != "mysql":
            return

        await conn.execute(
            text(
                "UPDATE ingest_jobs j LEFT JOIN knowledge_bases kb "
                "ON kb.id = j.knowledge_base_id "
                "SET j.knowledge_base_id = NULL "
                "WHERE j.knowledge_base_id IS NOT NULL AND kb.id IS NULL"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE documents "
                "MODIFY knowledge_base_id VARCHAR(36) NOT NULL"
            )
        )
        document_has_kb_fk = await conn.run_sync(
            lambda sync_conn: any(
                item.get("constrained_columns") == ["knowledge_base_id"]
                and item.get("referred_table") == "knowledge_bases"
                and item.get("referred_columns") == ["id"]
                for item in inspect(sync_conn).get_foreign_keys("documents")
            )
        )
        if not document_has_kb_fk:
            await conn.execute(
                text(
                    "ALTER TABLE documents "
                    "ADD CONSTRAINT fk_documents_knowledge_base "
                    "FOREIGN KEY (knowledge_base_id) "
                    "REFERENCES knowledge_bases(id) ON DELETE RESTRICT"
                )
            )
        job_has_kb_fk = await conn.run_sync(
            lambda sync_conn: any(
                item.get("constrained_columns") == ["knowledge_base_id"]
                and item.get("referred_table") == "knowledge_bases"
                and item.get("referred_columns") == ["id"]
                for item in inspect(sync_conn).get_foreign_keys("ingest_jobs")
            )
        )
        if not job_has_kb_fk:
            await conn.execute(
                text(
                    "ALTER TABLE ingest_jobs "
                    "ADD CONSTRAINT fk_ingest_jobs_knowledge_base "
                    "FOREIGN KEY (knowledge_base_id) "
                    "REFERENCES knowledge_bases(id) ON DELETE SET NULL"
                )
            )


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
