"""Jinja2 environment and the render helper used by all HTML routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.responses import Response

from lanpilot import __version__

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Messages shown after a redirect. Only these fixed texts can appear; the query parameter
# selects one and is never rendered itself.
NOTICES = {
    "signed_out": "You have been signed out.",
    "password_changed": "Your password has been changed. Other sessions were signed out.",
    "user_created": "The user has been created.",
    "user_updated": "The user has been updated.",
    "user_deleted": "The user has been deleted.",
    "password_reset": "The password has been set. The user's sessions were signed out.",
    "server_created": "The server has been added.",
    "server_updated": "The server has been saved.",
    "server_deleted": "The server has been deleted.",
    "map_changed": "The map change was sent to the server.",
    "restarted_soft": "The match was restarted.",
    "restarted_hard": "The level was reloaded.",
    "maps_refreshed": "The map list was read from the server again.",
    "image_saved": "The map image has been saved.",
    "image_removed": "The map image has been removed.",
}

# autoescape is on for every template, whatever its file extension.
_environment = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
TONES = 6


def tone(text: str) -> int:
    """Stable number 0..5 for a text; picks the placeholder colour of a map without image."""
    return sum(text.encode()) % TONES


_environment.filters["tone"] = tone
templates = Jinja2Templates(env=_environment)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    server = request.app.state.settings.server
    images = request.app.state.map_images

    def map_image_url(family: str, map_key: str) -> str | None:
        filename = images.filename(family, map_key)
        return f"{server.root_path}/map-images/{filename}" if filename else None

    values: dict[str, Any] = {
        "map_image_url": map_image_url,
        "auth": getattr(request.state, "auth", None),
        "root": server.root_path,
        "insecure_transport": not server.is_https,
        "notice": NOTICES.get(request.query_params.get("notice", "")),
        "version": __version__,
    }
    values.update(context or {})
    return templates.TemplateResponse(
        request, name, values, status_code=status_code, headers=headers
    )
