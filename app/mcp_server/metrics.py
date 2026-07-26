"""无额外运行时依赖的低基数 MCP Prometheus 指标。"""

from __future__ import annotations

import threading
from collections import Counter


class McpMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: Counter[tuple[str, str]] = Counter()
        self._latency_sum: Counter[str] = Counter()

    def observe(self, operation: str, status: str, latency_seconds: float) -> None:
        with self._lock:
            self._calls[(operation, status)] += 1
            self._latency_sum[operation] += max(0.0, latency_seconds)

    def render(self) -> str:
        with self._lock:
            calls = dict(self._calls)
            latency = dict(self._latency_sum)
        lines = [
            "# HELP trustguard_rag_mcp_requests_total MCP operations by outcome.",
            "# TYPE trustguard_rag_mcp_requests_total counter",
        ]
        for (operation, status), value in sorted(calls.items()):
            lines.append(
                "trustguard_rag_mcp_requests_total"
                f'{{operation="{operation}",status="{status}"}} {value}'
            )
        lines.extend(
            [
                "# HELP trustguard_rag_mcp_request_latency_seconds_sum "
                "Cumulative MCP operation latency.",
                "# TYPE trustguard_rag_mcp_request_latency_seconds_sum counter",
            ]
        )
        for operation, value in sorted(latency.items()):
            lines.append(
                "trustguard_rag_mcp_request_latency_seconds_sum"
                f'{{operation="{operation}"}} {value:.6f}'
            )
        return "\n".join(lines) + "\n"
