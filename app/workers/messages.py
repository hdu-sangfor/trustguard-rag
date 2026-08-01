"""Outbox 中继与消费者共用的版本化命令信封。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

INGEST_DOCUMENT = "document.ingest"
REVIEWED_INGEST_DOCUMENT = "document.ingest.reviewed"
CLEANUP_DOCUMENT = "document.cleanup"
CLEANUP_JOB_STAGING = "job.staging.cleanup"
RESOLVE_CONFLICT = "document.resolve"
RUN_CRAWLER = "crawler.run"

ROUTING_KEYS = {
    INGEST_DOCUMENT: "rag.ingest",
    REVIEWED_INGEST_DOCUMENT: "rag.ingest.reviewed",
    CLEANUP_DOCUMENT: "rag.cleanup",
    CLEANUP_JOB_STAGING: "rag.cleanup",
    RESOLVE_CONFLICT: "rag.resolve",
    RUN_CRAWLER: "rag.crawl",
}


@dataclass(frozen=True, slots=True)
class CommandMessage:
    event_id: str
    event_type: str
    aggregate_id: str
    payload: dict[str, Any]
    schema_version: int = 1

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "event_id": self.event_id,
                "event_type": self.event_type,
                "aggregate_id": self.aggregate_id,
                "payload": self.payload,
                "schema_version": self.schema_version,
            },
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, body: bytes) -> "CommandMessage":
        value = json.loads(body.decode("utf-8"))
        if value.get("schema_version") != 1:
            raise ValueError("unsupported command schema version")
        event_type = str(value["event_type"])
        if event_type not in ROUTING_KEYS:
            raise ValueError(f"unsupported command type: {event_type}")
        return cls(
            event_id=str(value["event_id"]),
            event_type=event_type,
            aggregate_id=str(value["aggregate_id"]),
            payload=dict(value.get("payload") or {}),
        )
