"""Games on the GoldSrc engine (Half-Life Dedicated Server): what they share.

Status comes from the server query, which needs no password; everything else goes through
RCON. Every GoldSrc game speaks the same engine commands (``maps *``, ``changelevel``,
``restart``, ``status``, ``kick``, ``addip``, ``stats``); the games differ in whether they
can restart a round. Protocol facts and how they were measured:
docs/games/counter-strike.md (the engine) and docs/games/valve.md (the other games).
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Sequence
from typing import ClassVar

from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    AdapterError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    Player,
    RestartMode,
    ServerStatus,
    UnsupportedError,
)
from lanpilot.core.transports.goldsrc_rcon import GoldSrcRcon
from lanpilot.games.valve.common import (
    MAP_NAME,
    MAX_NAME_LENGTH,
    STATS_INTERVAL_SECONDS,
    DiscoverValve,
    RconPlayerControl,
    ip_of,
    parse_stats,
    query_status,
)
from lanpilot.i18n import M

__all__ = [
    "GoldSrcAdapter",
    "GoldSrcConnection",
    "parse_maps",
    "parse_stats",
    "parse_status",
]

# A player in the answer to "status" (the engine's format "#%2i %8s %i %s" and so on):
# # 1 "Ada" 2 STEAM_0:1:23   5 01:23   25    0 10.0.0.5:27005
# slot, quoted name, user id, unique id ("BOT" for bots), frags, time, ping, loss, address.
# The name is matched greedily: whatever it contains, the line ends with the real fields.
STATUS_PLAYER = re.compile(
    r'#\s*\d+\s+"(?P<name>.*)"\s+(?P<userid>\d{1,9})\s+(?P<uniqueid>\S+)\s+'
    r"(?P<frags>-?\d{1,9})\s+[\d:]+\s+(?P<ping>\d{1,6})\s+\d+(?:\s+(?P<address>\S+))?\s*",
    re.ASCII,  # user ids go into commands: no other digits than 0-9
)

GOLDSRC_CAPABILITIES = frozenset(
    {
        Capability.STATUS,
        Capability.MAP_LIST,
        Capability.MAP_CHANGE,
        Capability.RESTART_HARD,
        Capability.LOAD_STATS,
        Capability.PLAYERS,
        Capability.KICK,
        Capability.BAN,
        Capability.CONSOLE,
        Capability.DISCOVER,
    }
)


class GoldSrcConnection(ConnectionSettings):
    port: int = Field(default=27015, title=M("Port"), ge=1, le=65535)
    rcon_password: SecretStr = Field(
        title=M("RCON password"),
        description=M("The value of rcon_password in the server's configuration (server.cfg)."),
    )


class GoldSrcAdapter(DiscoverValve, RconPlayerControl, GameAdapter):
    """A GoldSrc game. Not registered itself; a game sets ``id``, ``name``, ``family``,
    ``image_lookup`` and, if it can restart a round, ``soft_restart_command`` together
    with ``Capability.RESTART_SOFT``."""

    capabilities = GOLDSRC_CAPABILITIES
    connection_schema = GoldSrcConnection
    #: The command that restarts the round or match; ``None`` when the game has none.
    soft_restart_command: ClassVar[str | None] = None

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, GoldSrcConnection)  # noqa: S101 - see base class
        self._host = connection.host
        self._port = connection.port
        self._password = connection.rcon_password.get_secret_value()
        self._rcon_client: GoldSrcRcon | None = None
        self._load: dict[str, float] = {}
        self._load_checked_at: float | None = None

    def _rcon(self) -> GoldSrcRcon:
        # Created on first use: an unusable password must not keep the status from working.
        if self._rcon_client is None:
            self._rcon_client = GoldSrcRcon(self._host, self._port, self._password)
        return self._rcon_client

    async def query(self) -> ServerStatus:
        status = await query_status(self._host, self._port)
        return dataclasses.replace(status, load=await self._current_load())

    async def _current_load(self) -> dict[str, float]:
        now = time.monotonic()
        due = self._load_checked_at is None or now - self._load_checked_at >= STATS_INTERVAL_SECONDS
        if due:
            self._load_checked_at = now
            try:
                self._load = parse_stats(await self._rcon().execute("stats"))
            except AdapterError:
                # Status must keep working when RCON does not, e.g. with a wrong password.
                self._load = {}
        return dict(self._load)

    async def list_maps(self) -> Sequence[MapRef]:
        return [MapRef(name) for name in parse_maps(await self._rcon().execute("maps *"))]

    async def change_map(self, target: MapRef) -> None:
        if not MAP_NAME.fullmatch(target.key):
            raise CommandRejectedError(M("This map name cannot be sent to the server."))
        answer = await self._rcon().execute(f"changelevel {target.key}")
        if "changelevel failed" in answer:
            raise CommandRejectedError(M("The server does not have the map {key}.", key=target.key))

    async def restart(self, mode: RestartMode) -> None:
        # "restart" loads the current map again; the soft restart is the game's own.
        if mode is RestartMode.SOFT:
            if self.soft_restart_command is None:
                raise UnsupportedError(M("{game} cannot restart only the round", game=self.name))
            await self._rcon().execute(self.soft_restart_command)
            return
        await self._rcon().execute("restart")

    async def console(self, command: str) -> str:
        return await self._rcon().execute(command)

    async def _execute(self, command: str) -> str:
        return await self._rcon().execute(command)

    def _parse_status(self, answer: str) -> list[tuple[Player, str | None]]:
        return parse_status(answer)

    def _kick_command(self, user_id: str, message: str) -> str:
        # "kick #<userid> <message>": the player sees "Kicked by Console : <message>".
        return f"kick #{user_id} {message}".rstrip()


def parse_status(answer: str) -> list[tuple[Player, str | None]]:
    """Players and their IP addresses from the answer to ``status`` (HLTV proxies, which
    have no frags, are left out)."""
    players = []
    for line in answer.splitlines():
        match = STATUS_PLAYER.fullmatch(line.strip())
        if match is None:
            continue
        bot = match["uniqueid"] == "BOT"
        address = None if bot else ip_of(match["address"])
        player = Player(
            id=match["userid"],
            name=match["name"][:MAX_NAME_LENGTH],
            score=int(match["frags"]),
            ping=int(match["ping"]),
            bot=bot,
            can_ban=address is not None,
        )
        players.append((player, address))
    return players


def parse_maps(listing: str) -> list[str]:
    """Map names from the answer to ``maps *``, without duplicates, in the server's order."""
    names: dict[str, None] = {}
    for line in listing.splitlines():
        name, dot, extension = line.strip().rpartition(".")
        if dot and extension.lower() == "bsp" and MAP_NAME.fullmatch(name):
            names.setdefault(name)
    return list(names)
