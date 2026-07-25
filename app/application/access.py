"""知识能力的协议无关调用身份与最小权限上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class KnowledgeCallerType(StrEnum):
    GATEWAY = "gateway"
    MCP = "mcp"
    DEVELOPMENT = "development"


class KnowledgePermission(StrEnum):
    SEARCH = "knowledge.search"
    ANSWER = "knowledge.answer"
    MANAGE = "knowledge.manage"
    RESOURCE_READ = "knowledge.resource.read"


class KnowledgeAccessDenied(RuntimeError):
    """调用身份无权执行知识操作或访问指定知识库。"""


@dataclass(frozen=True)
class KnowledgeAccessContext:
    """进入知识应用服务前已经验证的调用身份。"""

    caller_type: KnowledgeCallerType
    service_id: str
    workspace_id: str
    permissions: frozenset[KnowledgePermission]
    allowed_knowledge_base_ids: frozenset[str] | None = None

    def require(
        self,
        permission: KnowledgePermission,
        *,
        knowledge_base_id: str | None = None,
    ) -> None:
        if permission not in self.permissions:
            raise KnowledgeAccessDenied(
                f"Service identity is not allowed to perform {permission.value}"
            )
        if (
            knowledge_base_id is not None
            and self.allowed_knowledge_base_ids is not None
            and knowledge_base_id not in self.allowed_knowledge_base_ids
        ):
            raise KnowledgeAccessDenied(
                "Service identity is not allowed to access this knowledge base"
            )


def gateway_access_context(
    *,
    service_id: str,
    workspace_id: str,
    development: bool = False,
) -> KnowledgeAccessContext:
    return KnowledgeAccessContext(
        caller_type=(
            KnowledgeCallerType.DEVELOPMENT
            if development
            else KnowledgeCallerType.GATEWAY
        ),
        service_id=service_id,
        workspace_id=workspace_id,
        permissions=frozenset(
            {
                KnowledgePermission.SEARCH,
                KnowledgePermission.ANSWER,
                KnowledgePermission.MANAGE,
            }
        ),
    )


def mcp_access_context(
    *,
    service_id: str,
    workspace_id: str,
) -> KnowledgeAccessContext:
    return KnowledgeAccessContext(
        caller_type=KnowledgeCallerType.MCP,
        service_id=service_id,
        workspace_id=workspace_id,
        permissions=frozenset(
            {
                KnowledgePermission.SEARCH,
                KnowledgePermission.RESOURCE_READ,
            }
        ),
    )

