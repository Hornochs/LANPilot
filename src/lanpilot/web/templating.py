"""Jinja2 environment and the render helper used by all HTML routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.responses import Response

from lanpilot import __version__, i18n
from lanpilot.auth import roles
from lanpilot.i18n import M

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

# Messages shown after a redirect. Only these fixed texts can appear; the query parameter
# selects one and is never rendered itself.
NOTICES = {
    "signed_out": M("You have been signed out."),
    "password_changed": M("Your password has been changed. Other sessions were signed out."),
    "language_changed": M("The language has been changed."),
    "user_created": M("The user has been created."),
    "user_updated": M("The user has been updated."),
    "user_deleted": M("The user has been deleted."),
    "password_reset": M("The password has been set. The user's sessions were signed out."),
    "server_created": M("The server has been added."),
    "server_updated": M("The server has been saved."),
    "server_deleted": M("The server has been deleted."),
    "map_changed": M("The map change was sent to the server."),
    "restarted_soft": M("The match was restarted."),
    "restarted_hard": M("The level was reloaded."),
    "maps_refreshed": M("The map list was read from the server again."),
    "image_saved": M("The map image has been saved."),
    "image_removed": M("The map image has been removed."),
    "player_kicked": M("The player was kicked."),
    "player_banned": M("The player was banned."),
    "map_added": M("The map was added to the list."),
    "map_removed": M("The map was removed from the list. The server's own list is unchanged."),
    "images_requested": M(
        "LANPilot is looking for images online. They appear as they are found;"
        " reload this page in a moment."
    ),
}

# autoescape is on for every template, whatever its file extension.
_environment = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    extensions=["jinja2.ext.i18n"],
)
# {{ _("text") }} and {% trans %} in templates; "newstyle" escapes and formats safely.
_environment.install_gettext_callables(  # type: ignore[attr-defined]
    i18n.gettext, i18n.ngettext, newstyle=True
)
TONES = 6


def tone(text: str) -> int:
    """Stable number 0..5 for a text; picks the placeholder colour of a map without image."""
    return sum(text.encode()) % TONES


_environment.filters["tone"] = tone
# {{ message | tr }}: a message created in code (an error, a label) in the page's language.
_environment.filters["tr"] = i18n.translate
templates = Jinja2Templates(env=_environment)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    settings = request.app.state.settings
    server = settings.server
    images = request.app.state.map_images
    auth = getattr(request.state, "auth", None)
    i18n.activate(
        i18n.choose(
            auth.user.language if auth is not None else None,
            request.headers.get("accept-language"),
            settings.default_language,
        )
    )

    def map_image_url(game: str, map_key: str) -> str | None:
        filename = images.filename(game, map_key)
        return f"{server.root_path}/map-images/{filename}" if filename else None

    values: dict[str, Any] = {
        "map_image_url": map_image_url,
        "auth": auth,
        "language": i18n.current(),
        "languages": i18n.LANGUAGES,
        "roles": roles.LABELS,
        "root": server.root_path,
        "insecure_transport": not server.is_https,
        "notice": NOTICES.get(request.query_params.get("notice", "")),
        "version": __version__,
    }
    values.update(context or {})
    return templates.TemplateResponse(
        request, name, values, status_code=status_code, headers=headers
    )
