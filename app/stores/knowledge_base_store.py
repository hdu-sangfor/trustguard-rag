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
from app.stores.db import get_engine
from app.stores.models import DocumentRow, KnowledgeBaseRow

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
            result = await session.execute(
                select(KnowledgeBaseRow, func.count(DocumentRow.id))
                .outerjoin(DocumentRow, DocumentRow.knowledge_base_id == KnowledgeBaseRow.id)
                .group_by(KnowledgeBaseRow.id)
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
        row = await self.get(knowledge_base_id)
        if row is None:
            return False
        if row.is_default or row.is_system:
            raise ValueError("System knowledge base cannot be deleted")
        if await self.document_count(knowledge_base_id):
            raise ValueError("Knowledge base is not empty")
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                delete(KnowledgeBaseRow).where(KnowledgeBaseRow.id == knowledge_base_id)
            )
            await session.commit()
            return bool(result.rowcount)


def get_knowledge_base_store() -> KnowledgeBaseStore:
    return KnowledgeBaseStore()
