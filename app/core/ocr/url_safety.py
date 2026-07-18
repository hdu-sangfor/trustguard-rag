"""OCR 出站 URL 安全校验（防 SSRF）。"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.core.ocr.errors import OcrError


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_safe_ocr_url(url: str, *, allow_private: bool = False) -> None:
    """校验 OCR API URL：仅 http(s)；默认拒绝解析到内网/回环地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise OcrError(f"OCR URL scheme not allowed: {parsed.scheme or '(empty)'}")
    host = parsed.hostname
    if not host:
        raise OcrError("OCR URL missing hostname")
    if allow_private:
        return
    try:
        # 字面量 IP
        ip = ipaddress.ip_address(host)
        if _is_blocked_ip(ip):
            raise OcrError(f"OCR URL targets blocked address: {host}")
        return
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise OcrError(f"OCR URL host cannot be resolved: {host}") from e
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise OcrError(f"OCR URL resolves to blocked address: {host} -> {ip}")
