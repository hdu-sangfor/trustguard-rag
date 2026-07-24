"""将统一过滤契约转换为各召回引擎的查询条件。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

ENTITY_IDS_ANY_FILTER = "entity_ids_any"


def iter_filter_fields(filters: dict[str, Any] | None) -> Iterator[tuple[str, Any]]:
    """展开顶层字段和受控元数据字段。"""
    if not filters:
        return
    for key, value in filters.items():
        if key == "metadata":
            for metadata_key, metadata_value in (value or {}).items():
                yield f"metadata.{metadata_key}", metadata_value
        else:
            yield key, value


def build_qdrant_filter(filters: dict[str, Any] | None) -> Filter | None:
    """生成 Qdrant 必须全部满足的载荷过滤条件。"""
    conditions = []
    for key, value in iter_filter_fields(filters):
        if key == ENTITY_IDS_ANY_FILTER:
            conditions.append(
                FieldCondition(key="entity_ids", match=MatchAny(any=list(value)))
            )
        else:
            conditions.append(
                FieldCondition(key=key, match=MatchValue(value=value))
            )
    return Filter(must=conditions) if conditions else None


def build_opensearch_filters(filters: dict[str, Any] | None) -> list[dict[str, Any]]:
    """生成不参与相关性评分的 OpenSearch 精确过滤条件。"""
    clauses = []
    for key, value in iter_filter_fields(filters):
        if key == ENTITY_IDS_ANY_FILTER:
            clauses.append({"terms": {"entity_ids": list(value)}})
        else:
            clauses.append({"term": {key: value}})
    return clauses


def matches_filters(item: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    """按生产引擎相同的字段语义过滤进程内模拟数据。"""
    for key, value in iter_filter_fields(filters):
        if key == ENTITY_IDS_ANY_FILTER:
            if not set(item.get("entity_ids") or ()) & set(value):
                return False
            continue
        if key.startswith("metadata."):
            actual = (item.get("metadata") or {}).get(key.removeprefix("metadata."))
        else:
            actual = item.get(key)
        if actual != value:
            return False
    return True
