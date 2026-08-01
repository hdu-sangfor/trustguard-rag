"""Crawler 出站 URL 安全校验。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """目标 URL 不允许由采集器访问。"""


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def validate_public_url(url: str, *, allow_private: bool = False) -> str:
    """校验协议、凭证和 DNS 解析结果，默认仅允许公网 HTTP(S)。"""
    candidate = (url or "").strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("Only http and https URLs are supported")
    if not parsed.hostname:
        raise UnsafeUrlError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URLs containing credentials are not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise UnsafeUrlError("URL contains an invalid port") from error

    if allow_private:
        return candidate

    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise UnsafeUrlError(f"Unable to resolve target hostname: {error}") from error
        addresses = {item[4][0].split("%", 1)[0] for item in infos}
        if not addresses:
            raise UnsafeUrlError("Target hostname did not resolve to an address")
        if any(not _is_public_address(address) for address in addresses):
            raise UnsafeUrlError("Private or special-purpose target addresses are not allowed")
    else:
        if not _is_public_address(str(literal)):
            raise UnsafeUrlError("Private or special-purpose target addresses are not allowed")
    return candidate
