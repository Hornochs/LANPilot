"""Half-Life Deathmatch and the GoldSrc mods: Team Fortress Classic, Day of Defeat, Opposing
Force and Counter-Strike: Condition Zero.

All of them are the GoldSrc base class with their own id, images and round restart. Measured
against real servers (HLDS build 10211): docs/games/valve.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from lanpilot.core.adapters import Capability
from lanpilot.games.valve.goldsrc import GOLDSRC_CAPABILITIES, GoldSrcAdapter

__all__ = [
    "ConditionZeroAdapter",
    "DayOfDefeatAdapter",
    "HalfLifeDeathmatchAdapter",
    "OpposingForceAdapter",
    "TeamFortressClassicAdapter",
]


class HalfLifeDeathmatchAdapter(GoldSrcAdapter):
    """No round restart: Half-Life has no command for one, only ``restart``."""

    id = "hldm"
    name = "Half-Life Deathmatch"
    family = "valve"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "hl", "gamebanana": "34"}


class TeamFortressClassicAdapter(GoldSrcAdapter):
    """No round restart, as on Half-Life."""

    id = "tfc"
    name = "Team Fortress Classic"
    family = "valve"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "tfc", "gamebanana": "284"}


class DayOfDefeatAdapter(GoldSrcAdapter):
    """Day of Defeat restarts a round with ``mp_clan_restartround``. A server without
    players keeps the value set (also across a map change) and restarts once a round runs,
    which does no harm; the restart itself has not been seen with players yet."""

    id = "dod"
    name = "Day of Defeat"
    family = "valve"
    capabilities = GOLDSRC_CAPABILITIES | {Capability.RESTART_SOFT}
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "dod", "gamebanana": "11"}
    soft_restart_command = "mp_clan_restartround 1"


class OpposingForceAdapter(GoldSrcAdapter):
    """No round restart, as on Half-Life. GameTracker has no Opposing Force images."""

    id = "opfor"
    name = "Half-Life: Opposing Force"
    family = "valve"
    image_lookup: ClassVar[Mapping[str, str]] = {"gamebanana": "262"}


class ConditionZeroAdapter(GoldSrcAdapter):
    """Condition Zero restarts a round like Counter-Strike 1.6."""

    id = "czero"
    name = "Counter-Strike: Condition Zero"
    family = "counterstrike"
    capabilities = GOLDSRC_CAPABILITIES | {Capability.RESTART_SOFT}
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "czero", "gamebanana": "7"}
    soft_restart_command = "sv_restart 1"
