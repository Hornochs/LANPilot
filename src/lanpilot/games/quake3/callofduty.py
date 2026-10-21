"""Call of Duty (1.5), Call of Duty 4 (CoD4X 1.8, which LAN parties run instead of the
stock 1.7 server) and Call of Duty: World at War (1.7).

Call of Duty keeps its multiplayer maps as bsp files in ``maps/mp``, so ``dir maps/mp bsp``
lists them. Call of Duty 4 and World at War load maps from fast files and cannot list them
(CoD4X has no ``dir``, WaW's finds nothing): their list is the stock maps from the
catalogue plus every map named in ``sv_mapRotation`` (where custom maps are configured).
None offers a ban: they ban by player id, which LAN players without a key share (measured
on CoD4X: bots all have id 0). Facts: docs/games/quake3.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from pydantic import Field

from lanpilot.core import catalog
from lanpilot.games.quake3.common import (
    MAP_NAME,
    Quake3Connection,
    Quake3FamilyAdapter,
    cvar_value,
    directory_maps,
    rotation_maps,
)
from lanpilot.i18n import M


class CallOfDutyConnection(Quake3Connection):
    port: int = Field(
        default=28960,
        title=M("Port"),
        description=M("The game port; status and RCON use it too."),
        ge=1,
        le=65535,
    )


class CallOfDutyAdapter(Quake3FamilyAdapter):
    id = "cod"
    name = "Call of Duty"
    connection_schema = CallOfDutyConnection
    game_types: ClassVar[Mapping[str, str]] = {
        "dm": "Deathmatch",
        "tdm": "Team Deathmatch",
        "sd": "Search and Destroy",
        "re": "Retrieval",
        "bel": "Behind Enemy Lines",
        "hq": "Headquarters",
    }
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "cod"}
    # No fast_restart in Call of Duty 1; map_restart keeps the players.

    async def _reported_maps(self) -> list[str]:
        return directory_maps(await self._execute("dir maps/mp bsp"))

    @classmethod
    def _is_mine(cls, info: Mapping[str, str]) -> bool:
        return info.get("gamename", "").lower() == "call of duty"


async def stock_and_rotation_maps(adapter: Quake3FamilyAdapter) -> list[str]:
    """The catalogue's stock maps of the game, then the maps of ``sv_mapRotation``."""
    names = dict.fromkeys(catalog.load().keys(adapter.id))
    rotation = cvar_value(await adapter._execute("sv_mapRotation"), "sv_mapRotation")
    names.update(dict.fromkeys(rotation_maps(rotation or "")))
    return [name for name in names if MAP_NAME.fullmatch(name)]


class CallOfDuty4Adapter(Quake3FamilyAdapter):
    id = "cod4"
    name = "Call of Duty 4: Modern Warfare"
    connection_schema = CallOfDutyConnection
    game_types: ClassVar[Mapping[str, str]] = {
        "dm": "Free-for-all",
        "war": "Team Deathmatch",
        "sd": "Search and Destroy",
        "sab": "Sabotage",
        "dom": "Domination",
        "koth": "Headquarters",
    }
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "cod4"}
    soft_restart = "fast_restart"  # the round, players and level stay
    hard_restart = "map_restart"  # reloads the level; players stay connected
    kick_takes_reason = True
    open_without_flag = "0"
    rcon_interval_seconds = 0.0  # CoD4X answers every command, however close

    async def _reported_maps(self) -> list[str]:
        return await stock_and_rotation_maps(self)

    @classmethod
    def _is_mine(cls, info: Mapping[str, str]) -> bool:
        return info.get("gamename", "").lower() == "call of duty 4"


class CallOfDutyWorldAtWarAdapter(Quake3FamilyAdapter):
    id = "codwaw"
    name = "Call of Duty: World at War"
    connection_schema = CallOfDutyConnection
    game_types: ClassVar[Mapping[str, str]] = {
        "dm": "Free-for-all",
        "tdm": "Team Deathmatch",
        "sd": "Search and Destroy",
        "sab": "Sabotage",
        "dom": "Domination",
        "koth": "Headquarters",
        "ctf": "Capture the Flag",
        "twar": "War",
    }
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "codww"}
    soft_restart = "fast_restart"
    hard_restart = "map_restart"  # reloads the level and keeps the game type
    # clientkick takes the slot only; with a reason it prints its usage and kicks nobody.

    async def _reported_maps(self) -> list[str]:
        return await stock_and_rotation_maps(self)

    @classmethod
    def _is_mine(cls, info: Mapping[str, str]) -> bool:
        return info.get("gamename", "").lower() == "call of duty: world at war"
