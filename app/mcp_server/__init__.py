"""TrustGuard RAG 的只读 MCP Gateway。"""

from app.mcp_server.server import create_mcp_app, create_mcp_server

__all__ = ["create_mcp_app", "create_mcp_server"]
