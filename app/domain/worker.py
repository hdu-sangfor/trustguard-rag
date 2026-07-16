"""可靠任务执行器和 Outbox 使用的领域协议值。"""

from __future__ import annotations

from enum import StrEnum


class CleanupAction(StrEnum):
    DELETE = "delete"
    ROLLBACK = "rollback"
    SUPERSEDE = "supersede"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    DEAD = "dead"


class ClaimOutcome(StrEnum):
    BUSY = "busy"
    TERMINAL = "terminal"
