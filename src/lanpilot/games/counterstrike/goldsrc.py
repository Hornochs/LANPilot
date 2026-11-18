"""Counter-Strike 1.6 (GoldSrc engine, Half-Life Dedicated Server).

Everything but the round restart comes from the GoldSrc base class. Protocol facts and how
they were measured: docs/games/counter-strike.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from lanpilot.core.adapters import Capability
from lanpilot.games.valve.goldsrc import (
    GOLDSRC_CAPABILITIES,
    GoldSrcAdapter,
    GoldSrcConnection,
    parse_maps,
    parse_stats,
    parse_status,
)

__all__ = [
    "CounterStrike16Adapter",
    "CounterStrike16Connection",
    "parse_maps",
    "parse_stats",
    "parse_status",
]

CounterStrike16Connection = GoldSrcConnection


class CounterStrike16Adapter(GoldSrcAdapter):
    id = "cs16"
    name = "Counter-Strike 1.6"
    family = "counterstrike"
    capabilities = GOLDSRC_CAPABILITIES | {Capability.RESTART_SOFT}
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "cs", "gamebanana": "4254"}
    # Restarts the round after one second and resets the scores.
    soft_restart_command = "sv_restart 1"
