# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared rate limiter instance — imported by main.py and auth router."""

import ipaddress

from slowapi import Limiter
from starlette.requests import Request


def _is_trusted_proxy(ip: str, networks: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(net, strict=False) for net in networks)
    except ValueError:
        return False


def _real_ip(request: Request) -> str:
    """Return the real client IP, trusting X-Real-IP only from known proxy addresses.

    X-Real-IP is accepted only when the direct TCP connection comes from a trusted
    proxy (nginx running on the same host or in the same Docker network). Direct
    backend access cannot spoof the rate-limit key.
    """
    from app.config import get_settings

    direct_ip = request.client.host if request.client else "127.0.0.1"
    if _is_trusted_proxy(direct_ip, get_settings().trusted_proxies):
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip.strip()
    return direct_ip


limiter = Limiter(key_func=_real_ip)
