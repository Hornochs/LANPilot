"""A simulated game server.

Lets people try LANPilot without a game server, and gives the user interface and the tests
something that exercises every capability and every error. Enabled with
``enable_demo_adapter``; it never opens a network connection.
"""

from __future__ import annotations

import asyncio
import dataclasses
import shlex
from collections.abc import Sequence
from typing import Literal

from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    Player,
    RestartMode,
    ServerStatus,
    ServerUnreachableError,
)

DEMO_PASSWORD = "demo"  # noqa: S105 - documented password of the simulation
MAPS = (
    MapRef("dm_foundry", "Foundry", "Deathmatch"),
    MapRef("dm_overgrown", "Overgrown", "Deathmatch"),
    MapRef("dm_reactor", "Reactor", "Deathmatch"),
    MapRef("ctf_twin_forts", "Twin Forts", "Capture the Flag"),
    MapRef("ctf_canyon", "Canyon", "Capture the Flag"),
    MapRef("race_a01", "A01 Race", "Race"),
)
PLAYER_NAMES = ("Ada", "Grace", "Linus", "Margaret", "Dennis", "Barbara")
BOT_INDEX = 3


class DemoConnection(ConnectionSettings):
    host: str = Field(
        default="demo.invalid",
        title="Host",
        description="Only a label: the simulation does not connect anywhere.",
        max_length=253,
    )
    port: int = Field(default=27015, title="Port", ge=1, le=65535)
    password: SecretStr = Field(
        title="Remote-control password",
        description=f"The simulated server accepts the password '{DEMO_PASSWORD}'.",
    )
    behaviour: Literal["normal", "slow", "offline"] = Field(
        default="normal",
        title="Behaviour",
        description="'slow' answers after a delay, 'offline' does not answer at all.",
    )
    players: int = Field(default=4, title="Simulated players", ge=0, le=len(PLAYER_NAMES))


class DemoAdapter(GameAdapter):
    id = "demo"
    name = "Demo server (simulated)"
    family = "demo"
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.MAP_CHANGE,
            Capability.RESTART_SOFT,
            Capability.RESTART_HARD,
            Capability.PLAYERS,
            Capability.KICK,
            Capability.CONSOLE,
            Capability.LOAD_STATS,
        }
    )
    connection_schema = DemoConnection

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, DemoConnection)  # noqa: S101 - checked by the base class
        self._settings = connection
        self._map = MAPS[0]
        # The fourth player is a bot that cannot be kicked, as on some real servers.
        self._players = [
            Player(
                id=str(index + 1),
                name=name,
                score=index * 3,
                ping=20 + index * 7,
                bot=index == BOT_INDEX,
                can_kick=index != BOT_INDEX,
            )
            for index, name in enumerate(PLAYER_NAMES[: connection.players])
        ]
        self.restarts: list[RestartMode] = []

    async def _connect(self, *, authenticated: bool = True) -> None:
        if self._settings.behaviour == "offline":
            raise ServerUnreachableError("The server did not answer.")
        if self._settings.behaviour == "slow":
            await asyncio.sleep(1.5)
        if authenticated and self._settings.password.get_secret_value() != DEMO_PASSWORD:
            raise AuthenticationError("The server refused the remote-control password.")

    async def query(self) -> ServerStatus:
        await self._connect(authenticated=False)  # status needs no password, as with real games
        return ServerStatus(
            name=f"Demo server {self._settings.host}:{self._settings.port}",
            players=len(self._players),
            max_players=len(PLAYER_NAMES),
            map=self._map,
            game_type=self._map.game_type,
            password_protected=False,
            load={"cpu": 7.5 + 2 * len(self._players), "fps": 512.0},
        )

    async def list_maps(self) -> Sequence[MapRef]:
        await self._connect()
        return MAPS

    async def change_map(self, target: MapRef) -> None:
        await self._connect()
        match = next((item for item in MAPS if item.key == target.key), None)
        if match is None:
            raise CommandRejectedError(f"The server has no map named {target.key!r}.")
        self._map = match

    async def restart(self, mode: RestartMode) -> None:
        await self._connect()
        self.restarts.append(mode)
        self._players = [dataclasses.replace(player, score=0) for player in self._players]

    async def list_players(self) -> Sequence[Player]:
        await self._connect()
        return list(self._players)

    async def kick(self, player: Player, reason: str = "") -> None:
        await self._connect()
        if not any(item.id == player.id for item in self._players):
            raise CommandRejectedError("That player is not on the server.")
        self._players = [item for item in self._players if item.id != player.id]

    async def console(self, command: str) -> str:
        await self._connect()
        words = shlex.split(command)
        if not words:
            return ""
        if words[0] == "status":
            return f"map: {self._map.key}\nplayers: {len(self._players)}"
        if words[0] == "echo":
            return " ".join(words[1:])
        return f"Unknown command: {words[0]}"
