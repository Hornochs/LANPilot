"""TrackMania Original, Sunrise, Sunrise eXtreme and Nations ESWC (GBXRemote 2, "challenges").

These games share one dedicated server binary (``/game=sunrise``, ``/game=nations``, …).
Unlike TrackMania Forever, the status methods answer without a login, so the status is read
without a password and a wrong password only affects remote control. The round restart is
``ChallengeRestart``, and there is no method to check whether the server can run a track.
Everything else is shared with TrackMania Forever in ``common.py``; protocol facts and how
they were measured (Sunrise eXtreme and Nations ESWC): docs/games/trackmania.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network
from typing import ClassVar

from lanpilot.core.adapters import Capability, DiscoveredServer
from lanpilot.games.trackmania.common import ChallengeAdapter
from lanpilot.games.trackmania.discovery import discover_sunrise


class TrackmaniaSunriseAdapter(ChallengeAdapter):
    id = "tmsunrise"
    name = "TrackMania Original, Sunrise, Nations ESWC"
    capabilities = ChallengeAdapter.capabilities | {Capability.DISCOVER}
    # The exchange sites of these games are gone. Many of their tracks were uploaded with the
    # original UId: Sunrise and Original tracks to the United site, Stadium tracks to the
    # Nations Forever site.
    image_lookup: ClassVar[Mapping[str, str]] = {"tmx": "tmuf.exchange,tmnf.exchange"}
    status_needs_login = False
    soft_restart_method = "ChallengeRestart"
    check_method = None

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        return await discover_sunrise(cls.id, networks, wait)
