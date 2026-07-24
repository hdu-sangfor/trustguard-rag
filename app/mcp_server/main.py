"""Uvicorn 入口：uvicorn app.mcp_server.main:app --port 18201。"""

from app.mcp_server.server import create_mcp_app

app = create_mcp_app()
