"""Unreal Tournament 2004 (Unreal Engine 2): status over the GameSpy query, control over the
WebAdmin. Shared with UT99 in ``webadmin.py``; protocol facts and how they were measured:
docs/games/unreal-tournament.md.

A change uses the WebAdmin's restart form, which loads map and game type together; the hard
restart sends that form with the running map and game type.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Network
from typing import ClassVar

from pydantic import Field, SecretStr

from lanpilot.core.adapters import Capability, DiscoveredServer, ProtocolError
from lanpilot.core.transports.uweb import parse
from lanpilot.games.unreal.common import GAME_CLASS, split_options
from lanpilot.games.unreal.discovery import discover_ut2004
from lanpilot.games.unreal.webadmin import ROOT, WebAdminAdapter, WebAdminConnection
from lanpilot.i18n import M


class UT2004Connection(WebAdminConnection):
    port: int = Field(
        default=8075,
        title=M("WebAdmin port"),
        description=M(
            "ListenPort in [UWeb.WebServer] of the server's ini file "
            "(LinuxGSM: 8075; the game's own default is 80)."
        ),
        ge=1,
        le=65535,
    )
    query_port: int = Field(
        default=7787,
        title=M("Query port"),
        description=M("The GameSpy query port: the game port plus 10 (7787 for game port 7777)."),
        ge=1,
        le=65535,
    )
    username: str = Field(
        default="admin",
        title=M("WebAdmin user"),
        description=M("With the standard access control the user is admin."),
        min_length=1,
        max_length=64,
    )
    password: SecretStr = Field(
        title=M("WebAdmin password"),
        description=M("AdminPassword in [Engine.AccessControl] of the server's ini file."),
    )


class UT2004Adapter(WebAdminAdapter):
    id = "ut2004"
    name = "Unreal Tournament 2004"
    connection_schema = UT2004Connection
    capabilities = WebAdminAdapter.capabilities | {Capability.DISCOVER}
    image_lookup: ClassVar[Mapping[str, str]] = {
        "unreal_archive": "Unreal Tournament 2004",
        "liandri": "Unreal Tournament 2004",
    }
    # XWebAdmin's CheckBox(): value "True". Bots have a kick box but no ban box.
    kick_box = ("Kick", "True")
    ban_box = ("Ban", "True")

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        return await discover_ut2004(cls.id, networks, wait)

    async def _travel(self, game_class: str, name: str, options: str) -> None:
        # The restart page's form loads the map with this game type. The rule overrides it
        # may offer as checkboxes ("Restart Conflicts") are left out, so the running values
        # stay as they are.
        self._expect_wait(
            await self._web.post(
                f"{ROOT}/server_restart",
                {"GameType": game_class, "MapName": name + options, "Save": "Accept"},
            )
        )

    async def _reload(self) -> None:
        page = parse(await self._web.get(f"{ROOT}/current_restart"))
        game_class, current = page.inputs.get("GameType", ""), page.inputs.get("MapName", "")
        if not GAME_CLASS.fullmatch(game_class) or not current:
            raise ProtocolError(M("The WebAdmin did not tell which game is running."))
        name, options = split_options(current)
        self._check(game_class, name, options)
        await self._travel(game_class, name, options)
