"""Unreal Tournament (UT99, Unreal Engine 1, also the OldUnreal 469 patches): status over the
GameSpy query, control over the WebAdmin. Shared with UT2004 in ``webadmin.py``; protocol
facts and how they were measured: docs/games/unreal-tournament.md.

The WebAdmin lists maps with their file extension (``DM-Deck16][.unr``). A change posts the
current game page with ``SwitchGameTypeAndMap``, the button the page offers after a game
type was chosen; ``SwitchMap`` would keep the running game type. The hard restart is a plain
request of ``current_restart``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network
from typing import ClassVar

from pydantic import Field, SecretStr

from lanpilot.core.adapters import Capability, DiscoveredServer
from lanpilot.games.unreal.discovery import discover_ut99
from lanpilot.games.unreal.webadmin import ROOT, WebAdminAdapter, WebAdminConnection


class UT99Connection(WebAdminConnection):
    port: int = Field(
        default=8076,
        title="WebAdmin port",
        description="ListenPort in [UWeb.WebServer] of the server's ini file "
        "(LinuxGSM: 8076; the game's own default is 80).",
        ge=1,
        le=65535,
    )
    query_port: int = Field(
        default=7778,
        title="Query port",
        description="The GameSpy query port: the game port plus 1 (7778 for game port 7777).",
        ge=1,
        le=65535,
    )
    username: str = Field(
        default="admin",
        title="WebAdmin user",
        description="AdminUsername in [UTServerAdmin.UTServerAdmin] of the server's ini file.",
        min_length=1,
        max_length=64,
    )
    password: SecretStr = Field(
        title="WebAdmin password",
        description="AdminPassword in [UTServerAdmin.UTServerAdmin] of the server's ini file.",
    )


class UT99Adapter(WebAdminAdapter):
    id = "ut99"
    name = "Unreal Tournament (UT99)"
    connection_schema = UT99Connection
    capabilities = WebAdminAdapter.capabilities | {Capability.DISCOVER}
    image_lookup: ClassVar[Mapping[str, str]] = {"unreal_archive": "Unreal Tournament"}
    map_extension = ".unr"
    # Bots have neither box: UT99's WebAdmin cannot remove them. A ban is by IP address.
    kick_box = ("KickPlayer", "kick")
    ban_box = ("BanPlayer", "ban")

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        return await discover_ut99(cls.id, networks, wait)

    async def _travel(self, game_class: str, name: str, options: str) -> None:
        self._expect_wait(
            await self._web.post(
                f"{ROOT}/current_game",
                {
                    "GameTypeSelect": game_class,
                    "MapSelect": name + self.map_extension + options,
                    "SwitchGameTypeAndMap": "Switch",
                },
            )
        )

    async def _reload(self) -> None:
        # Requesting this page restarts the current map; it is a link, not a form.
        self._expect_wait(await self._web.get(f"{ROOT}/current_restart"))
