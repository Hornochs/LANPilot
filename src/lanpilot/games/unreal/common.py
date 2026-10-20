"""What the Unreal Tournament adapters share: the GameSpy status query and the rules for
map names, game types and URL options. Protocol facts: docs/games/unreal-tournament.md."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping

from opengsq.protocols import GameSpy1

from lanpilot.core.adapters import MapRef, ProtocolError, ServerStatus, ServerUnreachableError

QUERY_TIMEOUT_SECONDS = 3.0
MAX_NAME_LENGTH = 100
# A map's file name without extension, e.g. "DM-Deck17", "CTF-Face][".
MAP_NAME = re.compile(r"[A-Za-z0-9_\-.\[\]]{1,64}")
# A game type class, e.g. "XGame.xDeathMatch", "Botpack.CTFGame".
GAME_CLASS = re.compile(r"[A-Za-z0-9_]{1,64}\.[A-Za-z0-9_]{1,64}")
# URL options that come with a map in a map list, e.g. "?LinkSetup=Default" (Onslaught).
# Strict, so a crafted list entry cannot add options such as ?Game= or ?AdminPassword=.
OPTIONS = re.compile(r"(?:\?[A-Za-z0-9_]{1,32}=[A-Za-z0-9_\-.]{1,64}){1,4}")
ALLOWED_OPTIONS = frozenset({"linksetup"})


def split_options(entry: str) -> tuple[str, str]:
    """``"ONS-Torlan?LinkSetup=Default"`` -> ``("ONS-Torlan", "?LinkSetup=Default")``."""
    name, question, rest = entry.partition("?")
    return name, f"?{rest}" if question else ""


def valid_options(options: str) -> bool:
    if not options:
        return True
    if not OPTIONS.fullmatch(options):
        return False
    keys = [part.split("=", 1)[0].lower() for part in options.split("?")[1:]]
    return all(key in ALLOWED_OPTIONS for key in keys)


async def query_status(
    host: str, port: int, game_names: Mapping[str, str] | None = None
) -> ServerStatus:
    """Status from the GameSpy query, which needs no password.

    ``game_names`` maps the lower-case short class name the query reports ("xdeathmatch")
    to the name users know ("DeathMatch").
    """
    try:
        # opengsq's timeout is per packet; a server that keeps sending would extend it.
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            status = await GameSpy1(host, port, QUERY_TIMEOUT_SECONDS).get_status()
    except (TimeoutError, OSError) as exc:
        raise ServerUnreachableError("The server did not answer the status query.") from exc
    except Exception as exc:
        raise ProtocolError("The server's status answer could not be read.") from exc
    info = {str(key).lower(): str(value) for key, value in dict(status.info).items()}

    current = info.get("mapname", "")
    title = info.get("maptitle", "").strip()
    game = info.get("gametype", "")
    return ServerStatus(
        name=info.get("hostname", "")[:MAX_NAME_LENGTH],
        players=_count(info.get("numplayers")),
        max_players=_count(info.get("maxplayers")),
        map=(
            MapRef(current, title[:MAX_NAME_LENGTH] if title and title.isprintable() else None)
            if MAP_NAME.fullmatch(current)
            else None
        ),
        game_type=(game_names or {}).get(game.lower(), game)[:MAX_NAME_LENGTH] or None,
        password_protected={"1": True, "true": True, "0": False, "false": False}.get(
            info.get("password", "").lower()
        ),
    )


def parse_count(text: str, *, signed: bool = False) -> int | None:
    """A number from a WebAdmin table cell, or ``None``."""
    pattern = r"-?\d{1,9}" if signed else r"\d{1,9}"
    return int(text) if re.fullmatch(pattern, text.strip()) else None


def _count(value: str | None) -> int:
    try:
        return max(0, int(value or 0))
    except ValueError:
        return 0
