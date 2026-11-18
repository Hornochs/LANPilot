"""Quake 3 Arena (id Tech 3, 1.32c and engines that kept its console, such as ioquake3).

Maps come from ``dir maps bsp``, which also lists the maps inside the pk3 archives. Game
types are numbers; the soft restart is ``map_restart``, the hard one loads the current map
again. A ban is by IP address (``addip``). Facts: docs/games/quake3.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from lanpilot.games.quake3.common import IpBanAdapter, directory_maps


class Quake3ArenaAdapter(IpBanAdapter):
    id = "q3a"
    name = "Quake III Arena"
    # 2 is the single-player mode, which a dedicated server cannot run.
    game_types: ClassVar[Mapping[str, str]] = {
        "0": "Free For All",
        "1": "Tournament",
        "3": "Team Deathmatch",
        "4": "Capture the Flag",
    }
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "q3"}

    async def _reported_maps(self) -> list[str]:
        return directory_maps(await self._execute("dir maps bsp"))

    @classmethod
    def _is_mine(cls, info: Mapping[str, str]) -> bool:
        version = info.get("version", "").lower()
        return "call of duty" not in info.get("gamename", "").lower() and version.startswith(
            ("q3 ", "ioq3", "quake3")
        )
