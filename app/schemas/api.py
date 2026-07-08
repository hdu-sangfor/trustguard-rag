"""通用 API 模型（健康检查、错误响应）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DepState(str, Enum):
    up = "up"
    down = "down"
    disabled = "disabled"  # 可选依赖未启用（如 MinIO），不计入降级


class DependencyStatus(BaseModel):
    status: DepState
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str = Field(description="ok=所有必需依赖可用；degraded=有必需依赖不可用")
    service: str
    version: str
    env: str
    dependencies: dict[str, DependencyStatus]


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
