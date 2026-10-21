"""ElDewrito (Halo Online): status from the HTTP status server, control over RCON.

Protocol facts and how they were measured: docs/games/eldewrito.md.

* The map list holds every map once per built-in game variant, like the Unreal and Quake 3
  adapters; ``extra["gametype"]`` is the variant's name as ``Game.GameType`` takes it.
* A map change goes back to the lobby first (``Game.Stop``), loads game variant and map
  there and starts the game (``Game.Start``) if anybody is on the server. Without players
  ``Game.Start`` fails and the server resets the lobby to its default map, so the choice is
  left in the lobby instead. The hard restart is the same without loading anything. There
  is no soft restart.
* Players come from ``Server.ListPlayers`` (slot, name, UID, IP address), scores and teams
  from the status server. Kick and ban go by slot: players on a LAN often share a UID (a
  copied game folder copies the key it is made from), and ``Server.KickUid`` would remove
  all of them.
* The LAN scan sends the query of the game's own server browser (from opengsq, as the
  Discord-Gameserver-Notifier does) to UDP 11774; servers answer from their game port with a
  packet of 160 bytes, and the status server on the game port plus one names them.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import Any

from opengsq.protocols.eldewrito import ElDewrito as OpenGsqElDewrito
from pydantic import Field, SecretStr

from lanpilot.core import discovery
from lanpilot.core.adapters import (
    AdapterError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    DiscoveredServer,
    GameAdapter,
    MapRef,
    Player,
    RestartMode,
    ServerStatus,
)
from lanpilot.core.transports.dewrito import DewRcon, get_info
from lanpilot.i18n import M

#: The built-in game variants, as ``Game.GameType`` takes them (case-sensitive; all 32 were
#: loaded on 0.7.1).
GAME_TYPES: tuple[str, ...] = (
    "Slayer",
    "Team Slayer",
    "Rockets",
    "Elimination",
    "Duel",
    "Oddball",
    "Team Oddball",
    "Lowball",
    "Ninjaball",
    "Rocketball",
    "Multi Flag",
    "One Flag",
    "Tank Flag",
    "Attrition CTF",
    "Assault",
    "Neutral Bomb",
    "One Bomb",
    "Attrition Bomb",
    "Infection",
    "Save One Bullet",
    "Alpha Zombie",
    "Hide and Seek",
    "Crazy King",
    "Team King",
    "Mosh Pit",
    "Juggernaut",
    "Mad Dash",
    "Ninjanaut",
    "VIP",
    "One Sided VIP",
    "Escort",
    "Influential VIP",
)
#: Team index -> colour, in Halo 3's order (unverified beyond red and blue).
TEAMS = ("Red", "Blue", "Green", "Orange", "Purple", "Gold", "Brown", "Pink")
GAME_TYPE_KEY = "gametype"  # MapRef.extra: the variant to load with the map
MAP_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")
SLOT = re.compile(r"[0-9]{1,2}", re.ASCII)
UID = re.compile(r"[0-9a-f]{16}")
# A line of Server.ListPlayers: [0] "Name" (uid: 0123456789abcdef, ip: 10.0.0.5:11774)
# The name may contain anything, quotes included, so the line is matched from both ends.
LIST_PLAYERS_ROW = re.compile(
    r'\[(?P<slot>[0-9]{1,2})\] "(?P<name>.*)" \(uid: (?P<uid>[0-9a-f]{16})'
    r"(?:, ip: (?P<ip>[^)\s]*))?\)"
)
LOBBY_MAP = "Current Map: mainmenu"  # Game.Info in the lobby
LOBBY_SECONDS = 20.0
LOBBY_POLL_SECONDS = 0.5
STATUS_SECONDS = 3.0
MAX_NAME_LENGTH = 100
MAX_MAPS = 500
MAX_PLAYERS = 64
REDACTED = "[redacted]"
# LAN scan: the query of the game's server browser, sent to the default game port.
SCAN_QUERY: bytes = OpenGsqElDewrito.BROADCAST_QUERY
SCAN_PORT = 11774
BEACON_PREFIX = SCAN_QUERY[:5]  # 01 "blam"
MIN_BEACON_BYTES = (
    121  # answers had 160 bytes; the query itself (23) comes back from LANPilot's own host
)
STATUS_PORT_OFFSET = 1  # Server.Port is Server.GamePort + 1 by default


class ElDewritoConnection(ConnectionSettings):
    port: int = Field(
        default=11776,
        title=M("RCON port"),
        description=M("Game.RconPort in the server's dewrito_prefs.cfg (default 11776, TCP)."),
        ge=1,
        le=65535,
    )
    status_port: int = Field(
        default=11775,
        title=M("Status port"),
        description=M(
            "Server.Port in the server's dewrito_prefs.cfg: its HTTP status server "
            "(default 11775, TCP)."
        ),
        ge=1,
        le=65535,
    )
    rcon_password: SecretStr = Field(
        title=M("RCON password"),
        description=M("Server.RconPassword in the server's dewrito_prefs.cfg."),
    )


@dataclass(frozen=True)
class PlayerRow:
    """One line of ``Server.ListPlayers``."""

    slot: str
    name: str
    uid: str
    ip: str | None  # without the port; None when the server printed none


def parse_list_players(answer: str) -> list[PlayerRow]:
    rows: list[PlayerRow] = []
    for line in answer.splitlines()[:MAX_PLAYERS]:
        match = LIST_PLAYERS_ROW.fullmatch(line.strip())
        if match is None:
            continue
        address = match["ip"] or ""
        ip = address.rpartition(":")[0] or address or None
        rows.append(PlayerRow(match["slot"], match["name"][:MAX_NAME_LENGTH], match["uid"], ip))
    return rows


def text(value: object, limit: int = MAX_NAME_LENGTH) -> str:
    """A printable string from the status JSON, or "" for anything else."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:limit].strip()


def number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -(10**9) < value < 10**9 else None


class ElDewritoAdapter(GameAdapter):
    id = "eldewrito"
    name = "ElDewrito (Halo Online)"
    family = "eldewrito"
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.MAP_CHANGE,
            Capability.RESTART_HARD,
            Capability.PLAYERS,
            Capability.KICK,
            Capability.BAN,
            Capability.CONSOLE,
            Capability.DISCOVER,
        }
    )
    connection_schema = ElDewritoConnection

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, ElDewritoConnection)  # noqa: S101 - see base class
        self._host = connection.host
        self._status_port = connection.status_port
        self._password = connection.rcon_password.get_secret_value()
        self._rcon_client: DewRcon | None = None

    def _rcon(self) -> DewRcon:
        # Created on first use: an unusable password must not keep the status from working.
        if self._rcon_client is None:
            self._rcon_client = DewRcon(self._host, self.connection.port, self._password)
        return self._rcon_client

    async def _execute(self, command: str) -> str:
        return await self._rcon().execute(command)

    async def close(self) -> None:
        if self._rcon_client is not None:
            await self._rcon_client.close()

    # --- status ------------------------------------------------------------------------

    async def query(self) -> ServerStatus:
        info = await get_info(self._host, self._status_port, seconds=STATUS_SECONDS)
        map_key = text(info.get("mapFile"))
        variant = text(info.get("variant"))
        if "passworded" in info:
            password_protected: bool | None = info.get("passworded") is True
        else:
            password_protected = False if "players" in info else None
        return ServerStatus(
            name=text(info.get("name")),
            players=max(number(info.get("numPlayers")) or 0, 0),
            max_players=max(number(info.get("maxPlayers")) or 0, 0),
            map=(
                MapRef(map_key, text(info.get("map")) or None)
                if MAP_NAME.fullmatch(map_key)
                else None
            ),
            # "none" in the lobby: the variant shows only while a game runs.
            game_type=variant if variant and variant.lower() != "none" else None,
            password_protected=password_protected,
        )

    # --- maps --------------------------------------------------------------------------

    async def list_maps(self) -> Sequence[MapRef]:
        answer = await self._execute("Game.ListMaps")
        names: dict[str, None] = {}
        for item in answer.split(","):
            name = item.strip()
            if MAP_NAME.fullmatch(name):
                names.setdefault(name)
        keys = list(names)[:MAX_MAPS]
        return [
            MapRef(name, None, game_type, {GAME_TYPE_KEY: game_type})
            for game_type in GAME_TYPES
            for name in keys
        ]

    async def change_map(self, target: MapRef) -> None:
        game_type = target.extra.get(GAME_TYPE_KEY)
        if not MAP_NAME.fullmatch(target.key) or (
            game_type is not None and game_type not in GAME_TYPES
        ):
            raise CommandRejectedError(M("This map or game type cannot be sent to the server."))
        await self._to_lobby()
        if game_type is not None:
            answer = await self._execute(f'Game.GameType "{game_type}"')
            if "loaded successfully" not in answer:
                raise CommandRejectedError(
                    M("The server cannot load the game type {name}.", name=game_type)
                )
        answer = await self._execute(f'Game.Map "{target.key}"')
        if "loaded successfully" not in answer:
            raise CommandRejectedError(M("The server does not have the map {key}.", key=target.key))
        await self._start()

    async def restart(self, mode: RestartMode) -> None:
        if mode is not RestartMode.HARD:
            await super().restart(mode)
        await self._to_lobby()
        await self._start()

    async def _in_lobby(self) -> bool:
        return LOBBY_MAP in await self._execute("Game.Info")

    async def _to_lobby(self) -> None:
        """Stop a running game; settings for the next one are made in the lobby."""
        if await self._in_lobby():
            return
        await self._execute("Game.Stop")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOBBY_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(LOBBY_POLL_SECONDS)
            if await self._in_lobby():
                return
        raise CommandRejectedError(M("The server did not return to the lobby."))

    async def _start(self) -> None:
        """Start the game set up in the lobby, if anybody is there to play it."""
        if not await self._rows():
            # Measured: without players Game.Start answers "Starting game...", fails, and
            # the lobby falls back to the default map, losing what was just set.
            return
        answer = await self._execute("Game.Start")
        if "Starting game" not in answer:
            raise CommandRejectedError(M("The server could not start the game."))

    # --- players -----------------------------------------------------------------------

    async def _rows(self) -> list[PlayerRow]:
        return parse_list_players(await self._execute("Server.ListPlayers"))

    async def _scoreboard(self) -> tuple[list[Mapping[str, Any]], bool]:
        """The status server's players and whether the game has teams; empty when it cannot
        be read. A server with a join password lists them only to requests that know it."""
        try:
            info = await get_info(self._host, self._status_port, seconds=STATUS_SECONDS)
            if info.get("passworded") is True:
                join_password = (await self._execute("Server.Password")).strip()
                if join_password:
                    info = await get_info(
                        self._host,
                        self._status_port,
                        join_password=join_password,
                        seconds=STATUS_SECONDS,
                    )
        except AdapterError:
            return [], False
        players = info.get("players")
        if not isinstance(players, list):
            return [], False
        entries: list[Mapping[str, Any]] = [
            item for item in players[:MAX_PLAYERS] if isinstance(item, dict)
        ]
        return entries, info.get("teams") is True

    async def list_players(self) -> Sequence[Player]:
        rows = await self._rows()
        entries, teams = await self._scoreboard()
        by_uid: dict[str, list[Mapping[str, Any]]] = {}
        by_name: dict[str, list[Mapping[str, Any]]] = {}
        for entry in entries:
            by_uid.setdefault(text(entry.get("uid")), []).append(entry)
            by_name.setdefault(text(entry.get("name")), []).append(entry)
        players = []
        for row in rows:
            # A UID shared by several players (a copied game folder) does not identify one.
            candidates = by_uid.get(row.uid, [])
            if len(candidates) != 1:
                candidates = by_name.get(row.name, [])
            entry = candidates[0] if len(candidates) == 1 else {}
            team = number(entry.get("team"))
            players.append(
                Player(
                    id=row.slot,
                    name=row.name,
                    score=number(entry.get("score")),
                    team=TEAMS[team] if teams and team is not None and 0 <= team < 8 else None,
                    can_ban=row.ip is not None,
                )
            )
        return players

    async def _still_there(self, player: Player) -> PlayerRow:
        """The row of ``player``. Slots are reused when players leave, so the name must
        still match before a slot number goes into a command."""
        if not SLOT.fullmatch(player.id) or int(player.id) > 15:
            raise CommandRejectedError(M("This player cannot be named in a command."))
        for row in await self._rows():
            if row.slot == player.id and row.name == player.name:
                return row
        raise CommandRejectedError(M("The player is no longer on the server."))

    async def kick(self, player: Player, reason: str = "") -> None:
        # ElDewrito's kick takes no reason.
        row = await self._still_there(player)
        answer = await self._execute(f"Server.KickIndex {row.slot}")
        if "Issued kick request" not in answer:
            raise CommandRejectedError(M("The server did not remove the player."))

    async def ban(self, player: Player, reason: str = "") -> None:
        row = await self._still_there(player)
        if row.ip is None:
            raise CommandRejectedError(
                M("The server did not report this player's IP address, so it cannot ban it.")
            )
        # Bans the address and kicks; the ban list is the server's banlist.txt.
        answer = await self._execute(f"Server.KickBanIndex {row.slot}")
        if "Added IP" not in answer:
            raise CommandRejectedError(M("The server did not remove the player."))

    # --- console -----------------------------------------------------------------------

    async def console(self, command: str) -> str:
        # "help" lists every variable with its value, the RCON password included.
        return (await self._execute(command)).replace(self._password, REDACTED)

    # --- LAN scan ----------------------------------------------------------------------

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        replies = await discovery.broadcast(networks, [(SCAN_QUERY, SCAN_PORT)], wait)
        answering: dict[tuple[str, int], None] = {}
        for reply in replies:
            if len(reply.data) >= MIN_BEACON_BYTES and reply.data.startswith(BEACON_PREFIX):
                answering.setdefault((reply.host, reply.port))

        async def describe(host: str, game_port: int) -> DiscoveredServer | None:
            status_port = game_port + STATUS_PORT_OFFSET
            if not 0 < status_port < 65536:
                return None
            info = await get_info(host, status_port, seconds=STATUS_SECONDS)
            if "eldewritoVersion" not in info:
                return None  # something else answers there
            variant = text(info.get("variant"))
            return DiscoveredServer(
                cls.id,
                host,
                None,  # the RCON port is not announced; the form keeps 11776
                name=discovery.clean_text(text(info.get("name"))),
                game_type=discovery.clean_text(variant) if variant.lower() != "none" else None,
                values={"status_port": str(status_port)},
            )

        return await discovery.gather_found(describe(host, port) for host, port in answering)
