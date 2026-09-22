# Copyright (c) 2025 Savvina AI Ltd
# Licensed under the Business Source License 1.1 — see LICENSE for details.

"""Shared "was the browser on HTTPS" helper.

Both the refresh-cookie Secure flag (routers/auth.py) and the HSTS response
header (main.py's SecurityHeadersMiddleware) need the same answer. It comes
from explicit configuration, not from ``X-Forwarded-Proto``: ``BEHIND_TLS_PROXY``
declares that a TLS-terminating reverse proxy fronts the stack, so every
request the backend sees was HTTPS at the browser. Without it, the scheme the
ASGI server itself accepted the connection as stands — plain HTTP in the
standard deployment, ``https`` only if uvicorn were handed a certificate.

``X-Forwarded-Proto`` is deliberately not consulted. On a plain-HTTP install it
is client-controllable — the bundled nginx used to pass whatever arrived on
port 3000 straight through, and nginx sits inside the trusted-proxy ranges — so
honouring it let any client flip its own cookie's Secure flag and receive HSTS.
Behind a real TLS proxy it fails the other way: a proxy that omits the header,
or sits outside ``TRUSTED_PROXIES``, silently yields a non-Secure long-lived
refresh token. An explicit flag has neither failure mode.

Operates on a raw ASGI ``scope`` rather than a ``starlette.requests.Request``
so both call sites can use it cheaply — a FastAPI ``Request`` exposes its
underlying scope via ``.scope``, and the pure-ASGI middleware already has one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def get_effective_scheme(scope: Mapping, behind_tls_proxy: bool) -> str:
    """Return the scheme the browser used, lowercased: ``"https"`` or ``"http"``.

    ``behind_tls_proxy`` (``Settings.behind_tls_proxy``) wins outright; otherwise
    ``scope["scheme"]`` — set by the ASGI server from the socket it accepted,
    with proxy-header rewriting explicitly disabled (``--no-proxy-headers`` in
    entrypoint.sh) so nothing downstream of the socket can overwrite it — is
    returned as-is.
    """
    if behind_tls_proxy:
        return "https"
    return str(scope.get("scheme", "http")).lower()
