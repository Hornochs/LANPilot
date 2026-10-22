"""Counter-Strike: Source, CS:GO and CS2 (Source and Source 2 engines, Source RCON).

Everything but CS2's workshop maps comes from the Source base class. Protocol facts and how
they were measured: docs/games/counter-strike.md. CS2 and CS:Source have been measured;
CS:GO is assumed to behave like CS:Source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from lanpilot.core.adapters import MapRef
from lanpilot.games.valve.common import MAP_NAME
from lanpilot.games.valve.source import (
    WORKSHOP,
    Source1PlayerControl,
    SourceAdapter,
    SourceConnection,
    parse_maps,
    parse_status,
)

__all__ = [
    "CounterStrike2Adapter",
    "CounterStrikeGlobalOffensiveAdapter",
    "CounterStrikeSourceAdapter",
    "SourceConnection",
    "parse_maps",
    "parse_status",
    "parse_workshop_maps",
]


class CounterStrikeSourceAdapter(Source1PlayerControl):
    id = "css"
    family = "counterstrike"
    name = "Counter-Strike: Source"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "css", "gamebanana": "2"}


class CounterStrikeGlobalOffensiveAdapter(Source1PlayerControl):
    id = "csgo"
    family = "counterstrike"
    name = "Counter-Strike: Global Offensive"
    image_lookup: ClassVar[Mapping[str, str]] = {
        "gametracker": "csgo",
        "gamebanana": "4660",
        "steam_app": "730",
    }


class CounterStrike2Adapter(SourceAdapter):
    """CS2 prints nothing for ``stats``, so it has no load values."""

    id = "cs2"
    family = "counterstrike"
    name = "Counter-Strike 2"
    # GameTracker has no CS2 section; its CS:GO pictures show the same layouts.
    image_lookup: ClassVar[Mapping[str, str]] = {
        "gametracker": "csgo",
        "gamebanana": "18134",
        "steam_app": "730",
    }
    cs2_listing = True

    async def list_maps(self) -> Sequence[MapRef]:
        stock = await super().list_maps()
        workshop = parse_workshop_maps(await self._rcon().execute("ds_workshop_listmaps"))
        # A downloaded workshop map shows up in "maps *" too, but only
        # ds_workshop_changelevel can load it, so the workshop entry wins.
        return [
            *(item for item in stock if item.key not in workshop),
            *(MapRef(name, extra={WORKSHOP: "1"}) for name in workshop),
        ]

    async def _is_workshop_map(self, name: str) -> bool:
        listing = await self._rcon().execute("ds_workshop_listmaps")
        return name in parse_workshop_maps(listing)


def parse_workshop_maps(listing: str) -> list[str]:
    """Map names from the answer to ``ds_workshop_listmaps``: one name per line."""
    names: dict[str, None] = {}
    for line in listing.splitlines():
        name = line.strip()
        if MAP_NAME.fullmatch(name):
            names.setdefault(name)
    return list(names)
