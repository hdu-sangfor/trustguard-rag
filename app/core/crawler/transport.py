"""HTTP transport that pins validated DNS results to the TCP connection."""

from __future__ import annotations

from collections.abc import Iterable

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

from app.core.crawler.safety import UnsafeUrlError, resolve_host_addresses


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve, validate, and connect to the same IP to prevent DNS rebinding."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._backend = backend or AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await resolve_host_addresses(
            host,
            port,
            allow_private=self._allow_private,
        )
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise UnsafeUrlError("Target hostname did not resolve to a usable address")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeUrlError("Unix sockets are not supported by the crawler")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class SafeAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport wired to the DNS-pinning network backend."""

    def __init__(
        self,
        *,
        allow_private: bool = False,
        limits: httpx.Limits = httpx.Limits(),
    ) -> None:
        super().__init__(limits=limits, trust_env=False)
        self._pool._network_backend = SafeNetworkBackend(  # type: ignore[attr-defined]
            allow_private=allow_private
        )
