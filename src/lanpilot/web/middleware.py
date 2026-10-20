"""ASGI middleware: host check, origin check and security headers.

Written as plain ASGI callables (not ``BaseHTTPMiddleware``) so they add no overhead to
streaming responses such as server-sent events.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HEALTH_PATH = "/healthz"

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
}


def hostname_of(host_header: str) -> str:
    """Host name without port, lower-case; handles IPv6 literals such as ``[::1]:9030``."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].partition("]")[0]
    return host.partition(":")[0]


def _log_safe(value: str) -> str:
    return value[:100].encode("ascii", "backslashreplace").decode()


class HostCheckMiddleware:
    """Reject requests whose ``Host`` header is not one LANPilot is published under.

    Protects against host header injection and DNS rebinding. The health endpoint is
    exempt so that local monitoring can call it by IP address.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Iterable[str], root_path: str = "") -> None:
        self.app = app
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.exempt_paths = frozenset({HEALTH_PATH, root_path + HEALTH_PATH})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        host_header = Headers(scope=scope).get("host", "")
        if hostname_of(host_header) not in self.allowed_hosts:
            logger.warning(
                "rejected request with Host %r; add it to server.allowed_hosts if it is legitimate",
                _log_safe(host_header),
            )
            response = PlainTextResponse("Invalid Host header.\n", status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class OriginCheckMiddleware:
    """Reject state-changing requests that do not come from LANPilot's own pages.

    Browsers send ``Origin`` with every cross-site POST, which makes this a CSRF defence that
    works before a session exists (the login form). Authenticated requests are additionally
    protected by the per-session CSRF token.

    A request passes if its origin equals ``public_origin``, or if it equals the ``Host`` it
    was sent to, which covers access through ``allowed_hosts``; that header has already
    passed the host check.
    """

    def __init__(self, app: ASGIApp, *, public_origin: str) -> None:
        self.app = app
        self.public_authority = urlsplit(public_origin).netloc.lower()

    def _allowed(self, source: str, host_header: str) -> bool:
        authority = urlsplit(source).netloc.lower()
        return bool(authority) and authority in (self.public_authority, host_header.lower())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        # Without Origin (some older browsers on same-origin form posts) fall back to Referer.
        source = headers.get("origin") or headers.get("referer") or ""
        if not self._allowed(source, headers.get("host", "")):
            logger.warning("rejected %s from origin %r", scope["method"], _log_safe(source))
            response = PlainTextResponse("Cross-site request rejected.\n", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """Refuse request bodies that are too large, before anything reads them.

    Requests that carry a body must announce its size: without ``Content-Length`` the limit
    could only be enforced while reading, when part of the work is already done. Browsers
    always send it.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is None or not declared.isdigit():
            response = PlainTextResponse("Content-Length required.\n", status_code=411)
        elif int(declared) > self.max_bytes:
            megabytes = self.max_bytes // (1024 * 1024)
            response = PlainTextResponse(
                f"The request is larger than {megabytes} MB.\n", status_code=413
            )
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)


class RootPathMiddleware:
    """Tells the routing the sub-path LANPilot is served from (``public_url``'s path), so it
    works whether or not the reverse proxy strips it. FastAPI's own ``root_path`` argument
    did the same and is deprecated."""

    def __init__(self, app: ASGIApp, *, root_path: str) -> None:
        self.app = app
        self.root_path = root_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.root_path and scope["type"] in ("http", "websocket"):
            scope["root_path"] = self.root_path
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, static_prefix: str) -> None:
        self.app = app
        self.static_prefix = static_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_static = scope["path"].startswith(self.static_prefix)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if not is_static:
                    # Pages contain account and server data; keep them out of shared caches
                    # and out of the browser's back/forward cache after logout.
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)
