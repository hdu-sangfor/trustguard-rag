"""增量入库 SyncRunner。

对照：
- LangChain indexes._api.index()：cleanup=none|full 与 added/skipped/updated/deleted 计数
- flexible-graphrag incremental_system：hash 短路 SKIP、ADD/UPDATE/DELETE 路由
发布仍走现有 IngestPipeline / request_delete，不旁路写索引。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.embedding.profiles import get_embedding_profile
from app.core.ingest.source_uri import normalize_conflict_policy, validate_source_uri
from app.domain import DocumentStatus, IngestJobStatus
from app.schemas.sync import SyncDirectory, SyncRequest, SyncResponse
from app.settings import get_settings
from app.stores.blob_store import get_blob_store
from app.stores.cursor_store import get_cursor_store
from app.stores.document_store import get_document_store
from app.stores.job_store import get_job_store
from app.stores.knowledge_base_store import get_knowledge_base_store
from app.stores.experience_store import is_experience_knowledge_base
from app.workers.eager import dispatch_eager

logger = logging.getLogger(__name__)

_TERMINAL_JOB = frozenset(
    {
        IngestJobStatus.SUCCEEDED,
        IngestJobStatus.DEDUPLICATED,
        IngestJobStatus.FAILED,
        IngestJobStatus.CANCELLED,
        IngestJobStatus.DISCARDED,
        IngestJobStatus.CONFLICT,
    }
)


@dataclass
class _PreparedItem:
    source_uri: str
    filename: str
    data: bytes
    content_hash: str
    mime: str | None = None
    collected_at: str | None = None


@dataclass
class _Enqueued:
    job_id: str
    is_update: bool


@dataclass
class _SyncCounters:
    added: int = 0
    skipped: int = 0
    updated: int = 0
    failed: int = 0
    deleted: int = 0
    job_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    seen_uris: set[str] = field(default_factory=set)
    enqueued: list[_Enqueued] = field(default_factory=list)


def parse_sync_roots(raw: str | None) -> dict[str, Path]:
    """解析 RAG_SYNC_ROOTS：JSON 对象或 key=path,key2=path2。"""
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        import json

        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("RAG_SYNC_ROOTS JSON must be an object")
        return {str(k): Path(str(v)).resolve() for k, v in data.items()}
    roots: dict[str, Path] = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Invalid RAG_SYNC_ROOTS entry: {part}")
        key, path = part.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("RAG_SYNC_ROOTS key must not be empty")
        roots[key] = Path(path.strip()).resolve()
    return roots


def resolve_sync_path(root_key: str, relative_path: str = ".") -> Path:
    """将 directory 请求解析到 allowlist 内的绝对路径。"""
    roots = parse_sync_roots(get_settings().sync_roots)
    if root_key not in roots:
        raise ValueError(f"Unknown sync root_key '{root_key}'")
    root = roots[root_key]
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Sync path escapes configured root") from exc
    return candidate


def _guess_mime(filename: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(filename)
    return guessed


def _expand_brace_glob(pattern: str) -> list[str]:
    """简易 {a,b} 展开，供 pathlib.glob 使用。"""
    if "{" not in pattern or "}" not in pattern:
        return [pattern]
    start = pattern.index("{")
    end = pattern.index("}", start)
    body = pattern[start + 1 : end]
    prefix = pattern[:start]
    suffix = pattern[end + 1 :]
    return [f"{prefix}{choice}{suffix}" for choice in body.split(",") if choice]


def _iter_directory_files(directory: SyncDirectory) -> list[_PreparedItem]:
    root = resolve_sync_path(directory.root_key, ".")
    base = resolve_sync_path(directory.root_key, directory.relative_path)
    if not base.exists():
        raise ValueError(f"Sync directory does not exist: {base}")
    files: list[Path] = []
    for glob_pat in _expand_brace_glob(directory.glob):
        files.extend(p for p in base.glob(glob_pat) if p.is_file())
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    items: list[_PreparedItem] = []
    for path in unique:
        rel = path.relative_to(root).as_posix()
        uri = directory.source_uri_template.format(
            root_key=directory.root_key,
            relative=rel,
        )
        data = path.read_bytes()
        items.append(
            _PreparedItem(
                source_uri=validate_source_uri(uri),
                filename=path.name,
                data=data,
                content_hash=hashlib.sha256(data).hexdigest(),
                mime=_guess_mime(path.name, None),
            )
        )
    return items


def _items_from_request(req: SyncRequest) -> list[_PreparedItem]:
    if req.items is not None:
        prepared: list[_PreparedItem] = []
        for item in req.items:
            data = base64.b64decode(item.content_b64, validate=True)
            prepared.append(
                _PreparedItem(
                    source_uri=validate_source_uri(item.source_uri),
                    filename=item.filename,
                    data=data,
                    content_hash=hashlib.sha256(data).hexdigest(),
                    mime=_guess_mime(item.filename, item.mime),
                    collected_at=item.collected_at,
                )
            )
        return prepared
    assert req.directory is not None
    return _iter_directory_files(req.directory)


async def _wait_jobs(job_ids: list[str], timeout_sec: float) -> dict[str, IngestJobStatus | None]:
    """轮询直到任务终态或超时。"""
    js = get_job_store()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    statuses: dict[str, IngestJobStatus | None] = {}
    pending = set(job_ids)
    while pending and loop.time() < deadline:
        done: list[str] = []
        for job_id in pending:
            job = await js.get(job_id)
            if not job:
                statuses[job_id] = None
                done.append(job_id)
                continue
            if job.status in _TERMINAL_JOB:
                statuses[job_id] = job.status
                done.append(job_id)
        for job_id in done:
            pending.discard(job_id)
        if pending:
            await asyncio.sleep(0.25)
    for job_id in pending:
        job = await js.get(job_id)
        statuses[job_id] = job.status if job else None
    return statuses


class SyncRunner:
    """执行一轮增量同步。"""

    async def run(self, req: SyncRequest) -> SyncResponse:
        policy = normalize_conflict_policy(req.conflict_policy)
        kb_store = get_knowledge_base_store()
        if req.knowledge_base_id:
            knowledge_base = await kb_store.resolve(req.knowledge_base_id)
        else:
            knowledge_base = await kb_store.get_default()
        if is_experience_knowledge_base(
            knowledge_base_id=knowledge_base.id,
            name=knowledge_base.name,
        ):
            raise ValueError("Experience knowledge base only accepts Experience APIs")
        profile = get_embedding_profile(knowledge_base.embedding_profile)

        prepared = _items_from_request(req)
        counters = _SyncCounters()
        sync_id = str(uuid4())
        documents = get_document_store()
        jobs = get_job_store()
        blobs = get_blob_store()

        for item in prepared:
            counters.seen_uris.add(item.source_uri)
            existing = await documents.find_by_source(
                "file",
                item.source_uri,
                item.content_hash,
                knowledge_base_id=knowledge_base.id,
            )
            if existing and existing.status == DocumentStatus.READY:
                counters.skipped += 1
                continue

            prior = await documents.find_ready_by_source_uri(
                item.source_uri,
                exclude_hash=item.content_hash,
                knowledge_base_id=knowledge_base.id,
            )
            is_update = bool(prior)

            job_id = str(uuid4())
            blobs.put_job_upload(job_id, item.data)
            options = {
                "original_filename": item.filename,
                "mime": item.mime,
                "knowledge_base_id": knowledge_base.id,
                "embedding_profile": profile.id,
                "embedding_provider": profile.provider,
                "embedding_api_driver": profile.api_driver,
                "embedding_model": profile.model,
                "embedding_dim": profile.dimension,
                "embedding_query_instruction": profile.query_instruction,
                "conflict_policy": policy,
                "source_uri": item.source_uri,
                "sync_id": sync_id,
            }
            if item.collected_at:
                options["collected_at"] = item.collected_at
            try:
                _, event = await jobs.create_ingest_command(
                    job_id=job_id,
                    source_type="file",
                    source=item.filename,
                    knowledge_base_id=knowledge_base.id,
                    options=options,
                )
            except Exception as exc:  # noqa: BLE001
                blobs.delete_job_staging(job_id)
                counters.failed += 1
                counters.errors.append(f"{item.source_uri}: create job failed: {exc}")
                continue
            await dispatch_eager(event)
            counters.job_ids.append(job_id)
            counters.enqueued.append(_Enqueued(job_id=job_id, is_update=is_update))
            if is_update:
                counters.updated += 1
            else:
                counters.added += 1

        if req.wait and counters.enqueued:
            statuses = await _wait_jobs(
                [e.job_id for e in counters.enqueued], req.wait_timeout_sec
            )
            added = updated = failed = 0
            extra_skipped = 0
            for entry in counters.enqueued:
                status = statuses.get(entry.job_id)
                if status == IngestJobStatus.SUCCEEDED:
                    if entry.is_update:
                        updated += 1
                    else:
                        added += 1
                elif status == IngestJobStatus.DEDUPLICATED:
                    extra_skipped += 1
                else:
                    failed += 1
                    counters.errors.append(f"job {entry.job_id}: {status}")
            counters.added = added
            counters.updated = updated
            counters.failed += failed
            counters.skipped += extra_skipped

        if req.cleanup == "full" and req.source_uri_prefix:
            prefix = req.source_uri_prefix.strip()
            orphans = await documents.find_ready_by_source_uri_prefix(
                prefix, knowledge_base_id=knowledge_base.id
            )
            for doc in orphans:
                if doc.source_uri in counters.seen_uris:
                    continue
                try:
                    event = await documents.request_delete(doc.id)
                    await dispatch_eager(event)
                    counters.deleted += 1
                except Exception as exc:  # noqa: BLE001
                    counters.failed += 1
                    counters.errors.append(f"delete {doc.id}: {exc}")

        cursor_value: str | None = None
        if req.cursor_key:
            fingerprint = hashlib.sha256(
                "|".join(
                    sorted(f"{i.source_uri}:{i.content_hash}" for i in prepared)
                ).encode("utf-8")
            ).hexdigest()[:32]
            cursor_value = (
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}:{fingerprint}"
            )
            await get_cursor_store().set(req.cursor_key, cursor_value)

        return SyncResponse(
            sync_id=sync_id,
            knowledge_base_id=knowledge_base.id,
            added=counters.added,
            skipped=counters.skipped,
            updated=counters.updated,
            failed=counters.failed,
            deleted=counters.deleted,
            job_ids=counters.job_ids,
            cursor_key=req.cursor_key,
            cursor_value=cursor_value,
            errors=counters.errors[:50],
        )


def get_sync_runner() -> SyncRunner:
    return SyncRunner()
