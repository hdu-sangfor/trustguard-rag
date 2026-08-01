"""知识库配置存储与默认知识库兼容逻辑。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding.profiles import (
    EmbeddingProfile,
    canonical_embedding_profile_id,
    get_embedding_profile,
)
from app.core.ingest.errors import CANCELLED
from app.domain import (
    DELETABLE_DOCUMENT_STATUSES,
    RESUMABLE_JOB_STATUSES,
    CleanupAction,
    DocumentStatus,
    IngestJobStatus,
    IngestStep,
)
from app.stores.db import get_engine
from app.stores.models import DocumentRow, IngestJobRow, KnowledgeBaseRow
from app.stores.outbox_store import OutboxEvent, add_outbox_event, event_from_row
from app.workers.messages import CLEANUP_DOCUMENT

DEFAULT_KNOWLEDGE_BASE_ID = str(uuid5(NAMESPACE_URL, "trustguard:knowledge-base:default"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class KnowledgeBaseStore:
    async def create(
        self,
        *,
        name: str,
        profile: EmbeddingProfile,
        description: str | None = None,
        knowledge_base_id: str | None = None,
        is_default: bool = False,
        is_system: bool = False,
    ) -> KnowledgeBaseRow:
        row = KnowledgeBaseRow(
            id=knowledge_base_id or str(uuid4()),
            name=name.strip(),
            description=description.strip() if description else None,
            embedding_profile=profile.id,
            embedding_provider=profile.provider,
            embedding_api_driver=profile.api_driver,
            embedding_model=profile.model,
            embedding_dim=profile.dimension,
            is_default=is_default,
            is_system=is_system,
        )
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ValueError("Knowledge base name already exists") from error
            await session.refresh(row)
        return row

    async def get(self, knowledge_base_id: str) -> KnowledgeBaseRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(KnowledgeBaseRow, knowledge_base_id)

    async def get_by_name(self, name: str) -> KnowledgeBaseRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.name == name.strip()
                )
            )
            return result.scalars().first()

    async def get_default(self) -> KnowledgeBaseRow:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(KnowledgeBaseRow, DEFAULT_KNOWLEDGE_BASE_ID)
            if row:
                return row
            result = await session.execute(
                select(KnowledgeBaseRow).where(KnowledgeBaseRow.is_default.is_(True))
            )
            row = result.scalars().first()
            if row:
                return row
        profile = get_embedding_profile("configured")
        try:
            return await self.create(
                name="默认知识库",
                description="由系统创建，用于兼容未指定知识库的历史请求。",
                profile=profile,
                knowledge_base_id=DEFAULT_KNOWLEDGE_BASE_ID,
                is_default=True,
                is_system=True,
            )
        except ValueError:
            row = await self.get(DEFAULT_KNOWLEDGE_BASE_ID)
            if row is None:
                raise
            return row

    async def ensure_profile_knowledge_base(self, profile_id: str) -> KnowledgeBaseRow:
        """为旧版请求提供按 profile 隔离的系统知识库。"""
        profile_id = canonical_embedding_profile_id(profile_id)
        if profile_id == "configured":
            return await self.get_default()
        knowledge_base_id = str(
            uuid5(NAMESPACE_URL, f"trustguard:knowledge-base:legacy:{profile_id}")
        )
        existing = await self.get(knowledge_base_id)
        if existing:
            return existing
        profile = get_embedding_profile(profile_id)
        try:
            return await self.create(
                name=f"兼容知识库 · {profile.label}"[:128],
                description="由旧版请求自动创建；建议新建正式知识库。",
                profile=profile,
                knowledge_base_id=knowledge_base_id,
                is_system=True,
            )
        except ValueError:
            existing = await self.get(knowledge_base_id)
            if existing is None:
                raise
            return existing

    async def resolve(self, knowledge_base_id: str | None) -> KnowledgeBaseRow:
        if not knowledge_base_id:
            return await self.get_default()
        row = await self.get(knowledge_base_id)
        if row is None:
            raise LookupError("Knowledge base not found")
        return row

    async def list(self) -> list[tuple[KnowledgeBaseRow, int]]:
        async with AsyncSession(get_engine()) as session:
            document_counts = (
                select(
                    DocumentRow.knowledge_base_id.label("knowledge_base_id"),
                    func.count(DocumentRow.id).label("document_count"),
                )
                .where(DocumentRow.knowledge_base_id.is_not(None))
                .group_by(DocumentRow.knowledge_base_id)
                .subquery()
            )
            result = await session.execute(
                select(
                    KnowledgeBaseRow,
                    func.coalesce(document_counts.c.document_count, 0),
                )
                .outerjoin(
                    document_counts,
                    document_counts.c.knowledge_base_id == KnowledgeBaseRow.id,
                )
                .order_by(KnowledgeBaseRow.is_default.desc(), KnowledgeBaseRow.created_at)
            )
            return [(row, int(count)) for row, count in result.all()]

    async def document_count(self, knowledge_base_id: str) -> int:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(func.count(DocumentRow.id)).where(
                    DocumentRow.knowledge_base_id == knowledge_base_id
                )
            )
            return int(result.scalar_one())

    async def update(
        self, knowledge_base_id: str, values: dict[str, object]
    ) -> KnowledgeBaseRow | None:
        values = {key: value for key, value in values.items() if key in {"name", "description"}}
        if isinstance(values.get("name"), str):
            values["name"] = str(values["name"]).strip()
        if isinstance(values.get("description"), str):
            values["description"] = str(values["description"]).strip() or None
        values["updated_at"] = _utcnow()
        async with AsyncSession(get_engine()) as session:
            try:
                result = await session.execute(
                    update(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == knowledge_base_id)
                    .values(**values)
                )
                if not result.rowcount:
                    await session.rollback()
                    return None
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ValueError("Knowledge base name already exists") from error
            return await session.get(KnowledgeBaseRow, knowledge_base_id)

    async def delete(self, knowledge_base_id: str) -> bool:
        async with AsyncSession(get_engine()) as session:
            row = (
                await session.execute(
                    select(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == knowledge_base_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.is_default or row.is_system:
                raise ValueError("System knowledge base cannot be deleted")

            document_count = int(
                (
                    await session.execute(
                        select(func.count(DocumentRow.id)).where(
                            DocumentRow.knowledge_base_id == knowledge_base_id
                        )
                    )
                ).scalar_one()
            )
            if document_count:
                raise ValueError("Knowledge base is not empty")

            active_job_count = int(
                (
                    await session.execute(
                        select(func.count(IngestJobRow.id)).where(
                            IngestJobRow.knowledge_base_id == knowledge_base_id,
                            IngestJobRow.status.in_(RESUMABLE_JOB_STATUSES),
                        )
                    )
                ).scalar_one()
            )
            if active_job_count:
                raise ValueError("Knowledge base has active ingest jobs")

            try:
                result = await session.execute(
                    delete(KnowledgeBaseRow).where(
                        KnowledgeBaseRow.id == knowledge_base_id
                    )
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ValueError("Knowledge base is still in use") from error
            return bool(result.rowcount)

    async def request_cascade_delete(
        self, knowledge_base_id: str
    ) -> tuple[bool, list[OutboxEvent]]:
        """删除空知识库，或原子地为其全部文档创建清理任务。

        返回 ``(True, [])`` 表示知识库已立即删除；返回 ``(False, events)``
        表示文档进入删除流程，最后一个文档清理完成后再删除知识库。
        """
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            row = (
                await session.execute(
                    select(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == knowledge_base_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("Knowledge base not found")
            if row.is_default or row.is_system:
                raise ValueError("System knowledge base cannot be deleted")

            active_jobs = list(
                (
                    await session.execute(
                        select(IngestJobRow)
                        .where(
                            IngestJobRow.knowledge_base_id == knowledge_base_id,
                            IngestJobRow.status.in_(RESUMABLE_JOB_STATUSES),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            now = _utcnow()
            for job in active_jobs:
                job.status = IngestJobStatus.CANCELLED
                job.current_step = IngestStep.CANCELLED
                job.error_code = CANCELLED
                job.error_message = (
                    "Task cancelled because its knowledge base was deleted"
                )
                job.finished_at = now
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.knowledge_base_id = None

            documents = list(
                (
                    await session.execute(
                        select(DocumentRow)
                        .where(DocumentRow.knowledge_base_id == knowledge_base_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            blocked = sorted(
                {
                    document.status.value
                    for document in documents
                    if document.status not in DELETABLE_DOCUMENT_STATUSES
                }
            )
            if blocked:
                raise ValueError(
                    "Knowledge base has documents that cannot be deleted while "
                    f"status is {', '.join(blocked)}"
                )

            if not documents:
                try:
                    await session.delete(row)
                    await session.commit()
                except IntegrityError as error:
                    await session.rollback()
                    raise ValueError("Knowledge base is still in use") from error
                return True, []

            events: list[OutboxEvent] = []
            had_ready_document = False
            for document in documents:
                had_ready_document = (
                    had_ready_document or document.status == DocumentStatus.READY
                )
                document.status = DocumentStatus.DELETING
                document.updated_at = _utcnow()
                event_row = add_outbox_event(
                    session,
                    event_type=CLEANUP_DOCUMENT,
                    aggregate_id=document.id,
                    payload={
                        "document_id": document.id,
                        "action": CleanupAction.DELETE,
                        "knowledge_base_id": knowledge_base_id,
                        "delete_knowledge_base": True,
                    },
                )
                events.append(event_from_row(event_row))
            if had_ready_document:
                row.content_revision += 1
            row.updated_at = _utcnow()
            await session.commit()
            return False, events

    async def try_finalize_delete(self, knowledge_base_id: str) -> bool:
        """最后一个文档清理完成后尝试删除其知识库，允许并发幂等调用。"""
        async with AsyncSession(get_engine()) as session:
            row = (
                await session.execute(
                    select(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == knowledge_base_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return True
            if row.is_default or row.is_system:
                return False
            remaining_documents = int(
                (
                    await session.execute(
                        select(func.count(DocumentRow.id)).where(
                            DocumentRow.knowledge_base_id == knowledge_base_id
                        )
                    )
                ).scalar_one()
            )
            if remaining_documents:
                return False
            active_jobs = int(
                (
                    await session.execute(
                        select(func.count(IngestJobRow.id)).where(
                            IngestJobRow.knowledge_base_id == knowledge_base_id,
                            IngestJobRow.status.in_(RESUMABLE_JOB_STATUSES),
                        )
                    )
                ).scalar_one()
            )
            if active_jobs:
                return False
            try:
                await session.delete(row)
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ValueError("Knowledge base is still in use") from error
            return True


def get_knowledge_base_store() -> KnowledgeBaseStore:
    return KnowledgeBaseStore()
