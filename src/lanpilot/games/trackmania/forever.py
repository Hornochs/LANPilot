"""TrackMania Forever: Nations Forever and United Forever (GBXRemote 2, "challenges").

There is no password-less status query: the status, too, needs a login. Map changes need
the level ``Admin``; ``SuperAdmin`` works as well but may also stop the server, so it is
not recommended. Everything else is shared with Sunrise in ``common.py``; protocol facts
and how they were measured: docs/games/trackmania.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network
from typing import ClassVar

from lanpilot.core.adapters import Capability, DiscoveredServer
from lanpilot.games.trackmania.common import (
    ChallengeAdapter,
    ChallengeConnection,
    plain,
    valid_filename,
)
from lanpilot.games.trackmania.discovery import discover_forever

__all__ = [
    "TrackmaniaForeverAdapter",
    "TrackmaniaForeverConnection",
    "plain",
    "valid_filename",
]

TrackmaniaForeverConnection = ChallengeConnection


class TrackmaniaForeverAdapter(ChallengeAdapter):
    id = "tmforever"
    name = "TrackMania Forever (Nations, United)"
    capabilities = ChallengeAdapter.capabilities | {Capability.DISCOVER}
    # Nations tracks are on tmnf.exchange, United ones on tmuf.exchange; a UId is unique.
    image_lookup: ClassVar[Mapping[str, str]] = {"tmx": "tmnf.exchange,tmuf.exchange"}
    status_needs_login = True
    soft_restart_method = "RestartChallenge"
    check_method = "CheckChallengeForCurrentServerParams"
    player_list_params = (1,)  # the Forever struct, without relayed servers
    message_parameter = True

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        return await discover_forever(cls.id, networks, wait)
