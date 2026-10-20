"""Application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from lanpilot import __version__
from lanpilot.auth.throttle import LoginThrottle
from lanpilot.config import Settings
from lanpilot.core import image_lookup, map_images, secrets
from lanpilot.core.actions import Actions
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.events import EventBus
from lanpilot.core.image_lookup import ImageLookup
from lanpilot.core.redaction import redactor
from lanpilot.core.runtime import ServerManager
from lanpilot.core.scan import Scanner
from lanpilot.db.engine import create_engine, create_sessionmaker
from lanpilot.web import (
    health,
    routes_account,
    routes_admin_audit,
    routes_admin_scan,
    routes_admin_servers,
    routes_admin_users,
    routes_auth,
    routes_events,
    routes_map_images,
    routes_servers,
)
from lanpilot.web.deps import ForbiddenError, NotAuthenticatedError
from lanpilot.web.middleware import (
    BodyLimitMiddleware,
    HostCheckMiddleware,
    OriginCheckMiddleware,
    SecurityHeadersMiddleware,
)
from lanpilot.web.templating import STATIC_DIR, render

logger = logging.getLogger(__name__)

# Room for the largest upload (a map image) plus the form around it.
MAX_BODY_BYTES = map_images.MAX_UPLOAD_BYTES + 1024 * 1024

ERROR_TITLES = {
    400: "Bad request",
    403: "Not allowed",
    404: "Page not found",
    405: "Method not allowed",
}


def _path_within_app(request: Request) -> str:
    """Request path without the root path, whether or not the proxy strips it."""
    root: str = request.app.state.settings.server.root_path
    path: str = request.scope["path"]
    if root and path.startswith(root + "/"):
        path = path[len(root) :]
    return path


async def _not_authenticated(request: Request, _exc: Exception) -> Response:
    if request.method not in ("GET", "HEAD") or "hx-request" in request.headers:
        return PlainTextResponse("Sign in required.\n", status_code=401)
    target = _path_within_app(request)
    if request.url.query:
        target += "?" + request.url.query
    root = request.app.state.settings.server.root_path
    location = f"{root}/login" if target == "/" else f"{root}/login?next={quote(target, safe='')}"
    return RedirectResponse(location, status_code=303)


async def _forbidden(request: Request, exc: Exception) -> Response:
    message = str(exc) or "You are not allowed to do this."
    return render(
        request, "error.html", {"title": ERROR_TITLES[403], "message": message}, status_code=403
    )


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for this type only
    title = ERROR_TITLES.get(exc.status_code, f"Error {exc.status_code}")
    message = "The request could not be completed."
    if exc.status_code == 404:
        message = "This page does not exist."
    return render(
        request,
        "error.html",
        {"title": title, "message": message},
        status_code=exc.status_code,
        headers=dict(exc.headers) if exc.headers else None,
    )


def create_app(settings: Settings) -> FastAPI:
    """Build the application. Raises ``ConfigError`` if no encryption key is configured."""
    key = secrets.load_key(settings)
    if settings.secret_key is not None:
        redactor.add(settings.secret_key.get_secret_value())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_path)
        sessionmaker = create_sessionmaker(engine)
        manager = ServerManager(
            sessionmaker=sessionmaker,
            registry=app.state.registry,
            box=app.state.secret_box,
            bus=app.state.bus,
            interval=settings.polling.interval_seconds,
            timeout=settings.polling.timeout_seconds,
        )
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.manager = manager
        app.state.actions = Actions(manager, sessionmaker, console_enabled=settings.enable_console)
        lookup = ImageLookup(
            settings.images,
            index=app.state.map_images,
            sessionmaker=sessionmaker,
            data_dir=settings.data_dir,
            registry=app.state.registry,
            on_found=manager.announce,
        )
        app.state.image_lookup = lookup
        manager.on_map = lookup.want
        try:
            async with sessionmaker() as db:
                await app.state.map_images.load(db)
                await image_lookup.forget_not_found(db)
            orphans = map_images.sweep(
                settings.data_dir / map_images.DIRECTORY_NAME, app.state.map_images
            )
            if orphans:
                logger.info("deleted %d map image files no longer in use", orphans)
            lookup.start()
            await manager.start()
        except Exception:
            # E.g. the schema is missing: serve anyway, so /healthz can report the problem.
            logger.exception("could not load the configured servers")
        try:
            yield
        finally:
            await manager.stop()
            await lookup.stop()
            await engine.dispose()

    server = settings.server
    app = FastAPI(
        title="LANPilot",
        version=__version__,
        lifespan=lifespan,
        root_path=server.root_path,
        # No interactive API documentation: LANPilot has no public API.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.login_throttle = LoginThrottle()
    app.state.secret_box = secrets.SecretBox(key)
    app.state.registry = load_registry()
    app.state.bus = EventBus()
    app.state.map_images = map_images.MapImageIndex()
    app.state.scanner = Scanner(app.state.registry, settings.scan)
    app.state.event_streams = routes_events.StreamLimit()

    # Middleware added last runs first: headers wrap everything, then the host check, then
    # the origin check (which relies on the Host header having been validated), then the
    # body size limit.
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    app.add_middleware(OriginCheckMiddleware, public_origin=server.public_origin)
    app.add_middleware(
        HostCheckMiddleware,
        allowed_hosts=[server.public_host, *server.allowed_hosts],
        root_path=server.root_path,
    )
    app.add_middleware(SecurityHeadersMiddleware, static_prefix=server.root_path + "/static/")

    app.add_exception_handler(NotAuthenticatedError, _not_authenticated)
    app.add_exception_handler(ForbiddenError, _forbidden)
    app.add_exception_handler(StarletteHTTPException, _http_error)

    app.include_router(health.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_account.router)
    app.include_router(routes_admin_users.router)
    app.include_router(routes_admin_servers.router)
    app.include_router(routes_admin_audit.router)
    app.include_router(routes_admin_scan.router)
    app.include_router(routes_servers.router)
    app.include_router(routes_events.router)
    app.include_router(routes_map_images.router)
    app.include_router(routes_map_images.admin_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
