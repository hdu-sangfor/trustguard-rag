"""CVE、CWE 与 CAPEC 安全实体的规范化和索引字段生成。"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any, Iterable

_CVE_PATTERN = re.compile(
    r"(?<![A-Z0-9])CVE(?:\s*[-_:]\s*|\s+)"
    r"(?P<year>\d{4})\s*[-_:]\s*(?P<number>\d{4,})(?!\d)",
    re.IGNORECASE,
)
_SIMPLE_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?P<kind>CWE|CAPEC)(?:\s*[-_:]\s*|\s+)"
    r"(?P<number>\d{1,7})(?!\d)",
    re.IGNORECASE,
)
_MARKDOWN_TITLE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_PRIMARY_METADATA_KEYS = (
    "entity_id",
    "normalized_identifier",
    "canonical_id",
    "document_identifier",
)
_TITLE_METADATA_KEYS = ("title", "name", "document_title")
_ALIAS_METADATA_KEYS = ("aliases", "alias")


def extract_security_entity_ids(*values: object) -> list[str]:
    """按首次出现顺序提取并规范化 CVE/CWE/CAPEC 编号。"""
    found: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value)
        matches: list[tuple[int, str]] = []
        for match in _CVE_PATTERN.finditer(text):
            matches.append(
                (
                    match.start(),
                    f"CVE-{match.group('year')}-{match.group('number')}",
                )
            )
        for match in _SIMPLE_PATTERN.finditer(text):
            matches.append(
                (
                    match.start(),
                    f"{match.group('kind').upper()}-{match.group('number')}",
                )
            )
        matches.sort(key=lambda item: item[0])
        found.extend(entity_id for _, entity_id in matches)

    return list(dict.fromkeys(found))


def entity_type(entity_id: str | None) -> str | None:
    """把规范化编号映射为稳定的业务类型。"""
    if not entity_id:
        return None
    prefix = entity_id.partition("-")[0]
    return {
        "CVE": "vulnerability",
        "CWE": "weakness",
        "CAPEC": "attack_pattern",
    }.get(prefix)


def build_security_entity_fields(
    *,
    text: str,
    original_filename: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成供 Qdrant 与 OpenSearch 共用的安全实体字段。"""
    metadata = metadata or {}
    title = _metadata_text(metadata, _TITLE_METADATA_KEYS) or _title_from_text(text)
    if not title and original_filename:
        title = PurePath(original_filename).stem

    explicit_primary = extract_security_entity_ids(
        *(_metadata_values(metadata, _PRIMARY_METADATA_KEYS))
    )
    filename_ids = extract_security_entity_ids(original_filename)
    title_ids = extract_security_entity_ids(title)
    text_ids = extract_security_entity_ids(text)
    primary_id = next(
        iter((*explicit_primary, *filename_ids, *title_ids, *text_ids)),
        None,
    )
    entity_ids = list(
        dict.fromkeys(
            (*explicit_primary, *filename_ids, *title_ids, *text_ids)
        )
    )
    aliases = list(
        dict.fromkeys(
            (
                *_metadata_aliases(metadata),
                *(entity_ids if primary_id else ()),
            )
        )
    )
    entity_types = list(
        dict.fromkeys(
            kind for kind in (entity_type(item) for item in entity_ids) if kind
        )
    )

    return {
        "entity_id": primary_id,
        "entity_type": entity_type(primary_id),
        "entity_ids": entity_ids,
        "entity_types": entity_types,
        "title": title,
        "aliases": aliases,
    }


def exact_entity_match_priority(
    item: dict[str, Any], query_entity_ids: Iterable[str]
) -> int:
    """返回精确实体匹配优先级：主实体高于关联实体。"""
    wanted = set(query_entity_ids)
    if not wanted:
        return 0
    if item.get("entity_id") in wanted:
        return 2

    indexed_ids = set(item.get("entity_ids") or ())
    if indexed_ids & wanted:
        return 1

    fallback = build_security_entity_fields(
        text=str(item.get("text") or ""),
        original_filename=item.get("original_filename"),
        metadata=item.get("metadata") or {},
    )
    if fallback["entity_id"] in wanted:
        return 2
    return 1 if set(fallback["entity_ids"]) & wanted else 0


def _metadata_values(
    metadata: dict[str, Any], keys: Iterable[str]
) -> list[object]:
    return [metadata[key] for key in keys if metadata.get(key) is not None]


def _metadata_text(metadata: dict[str, Any], keys: Iterable[str]) -> str | None:
    values = _metadata_values(metadata, keys)
    return str(values[0]).strip() if values and str(values[0]).strip() else None


def _metadata_aliases(metadata: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for value in _metadata_values(metadata, _ALIAS_METADATA_KEYS):
        if isinstance(value, (list, tuple, set)):
            aliases.extend(str(item).strip() for item in value)
        else:
            aliases.extend(part.strip() for part in str(value).split(","))
    return [item for item in aliases if item]


def _title_from_text(text: str) -> str | None:
    match = _MARKDOWN_TITLE.search(text)
    return match.group(1).strip() if match else None
