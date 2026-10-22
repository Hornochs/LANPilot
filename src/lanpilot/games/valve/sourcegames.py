"""Day of Defeat: Source, Half-Life 2: Deathmatch and Team Fortress 2.

All of them are the Source base class with player control, as on CS:Source: their own id
and images, ``mp_restartgame 1`` as the round restart. Measured against real servers:
docs/games/valve.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from lanpilot.games.valve.source import Source1PlayerControl

__all__ = [
    "DayOfDefeatSourceAdapter",
    "HalfLife2DeathmatchAdapter",
    "TeamFortress2Adapter",
]


class DayOfDefeatSourceAdapter(Source1PlayerControl):
    id = "dods"
    name = "Day of Defeat: Source"
    family = "valve"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "dods", "gamebanana": "10"}


class HalfLife2DeathmatchAdapter(Source1PlayerControl):
    id = "hl2dm"
    name = "Half-Life 2: Deathmatch"
    family = "valve"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "hl2dm", "gamebanana": "5"}


class TeamFortress2Adapter(Source1PlayerControl):
    id = "tf2"
    name = "Team Fortress 2"
    family = "valve"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "tf2", "gamebanana": "297"}
