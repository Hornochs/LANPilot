"""What the games of the Quake 3 engine family share: status from ``getstatus``, control
over RCON, maps per game type, players from ``status`` and the LAN scan.

Protocol facts and how they were measured: docs/games/quake3.md. Each game sets:

* ``game_types``: game type code (the value of ``g_gametype``) -> display name. The map
  list holds every map once per game type, like the Unreal adapters, and a change sets
  ``g_gametype`` before loading the map;
* ``_reported_maps()``: the maps the server has (``dir`` where the engine has it, the
  catalogue and the map rotation where it does not);
* the restart commands and the kick command; ``BAN`` only where the game can ban by IP
  address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import ClassVar

from pydantic import Field, SecretStr

from lanpilot.core import discovery
from lanpilot.core.adapters import (
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    DiscoveredServer,
    GameAdapter,
    MapRef,
    Player,
    ProtocolError,
    RestartMode,
    ServerStatus,
)
from lanpilot.core.transports.quake3 import (
    HEADER,
    Quake3Rcon,
    get_status,
    parse_status_response,
    strip_colors,
)
from lanpilot.i18n import M

# Map names as they occur in practice ("q3dm17", "pro-q3dm6", "mp_crash_snow"). Everything
# that goes into a console command is checked against this.
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
GAME_TYPE_CODE = re.compile(r"[A-Za-z0-9_]{1,32}")
SLOT = re.compile(r"[0-9]{1,3}", re.ASCII)
GAME_TYPE_KEY = "gametype"  # MapRef.extra: the g_gametype value to set
QUERY_TIMEOUT_SECONDS = 3.0
LEVEL_LOAD_SECONDS = 15.0
MAX_NAME_LENGTH = 100
MAX_MAPS = 2000
MAX_STATUS_COLUMNS = 8  # fields on either side of the name in a status row (CoD4X: 5 and 4)
# What a kick reason may contain: it becomes part of a console command. CoD4X itself
# forbids " ; % / \ in reasons.
UNSAFE_IN_MESSAGE = re.compile(r"[^A-Za-z0-9 .,!?()_-]")
MAX_MESSAGE_LENGTH = 64
# "Can't find map maps/nosuchmap.bsp" (Quake 3, CoD 1); "^1Error: Can't find map x" (CoD4X).
MISSING_MAP = "Can't find map"
# A cvar as the console prints it: "name" is:"value^7" default:"..." (Quake 3, CoD 1) or
# "name" is: "value^7" default: "..." (CoD4X).
CVAR_VALUE = re.compile(r'"(?P<name>[A-Za-z0-9_]{1,64})" is: ?"(?P<value>[^"]*)"')


class Quake3Connection(ConnectionSettings):
    port: int = Field(
        default=27960,
        title=M("Port"),
        description=M("The game port; status and RCON use it too."),
        ge=1,
        le=65535,
    )
    rcon_password: SecretStr = Field(
        title=M("RCON password"),
        description=M("The value of rconpassword (rcon_password on CoD4) in the server's config."),
    )


@dataclass(frozen=True)
class StatusEntry:
    """One row of the answer to ``status``."""

    slot: str
    name: str  # colour codes removed
    score: int | None
    ping: int | None
    address: str


def parse_status(answer: str) -> list[StatusEntry]:
    """The player rows of ``status``.

    The engines differ in their columns (Quake 3: num score ping name lastmsg address qport
    rate; CoD 1 adds guid before the name, CoD4X playerid and steamid), so the header line
    says how many fields come before and after the name. The name itself may contain
    spaces; the fields around it never do.
    """
    before = after = None
    rows: list[StatusEntry] = []
    for line in answer.splitlines():
        words = line.split()
        if words[:3] == ["num", "score", "ping"] and "name" in words:
            before = words.index("name")
            after = len(words) - before - 1
            if before > MAX_STATUS_COLUMNS or after > MAX_STATUS_COLUMNS:
                before = after = None  # not a header any engine prints
            continue
        if before is None or after is None or not words or not SLOT.fullmatch(words[0]):
            continue
        if len(words) < before + after:
            continue
        match = re.fullmatch(
            r"\s*" + r"(\S+)\s+" * before + r"(.*?)" + r"\s+(\S+)" * after + r"\s*", line
        )
        if match is None:
            continue
        fields = match.groups()
        head, name, tail = fields[:before], fields[before], fields[before + 1 :]
        rows.append(
            StatusEntry(
                slot=head[0],
                name=strip_colors(name).strip()[:MAX_NAME_LENGTH],
                score=_int(head[1]),
                ping=_int(head[2]),  # "CNCT" or "ZMBI" while connecting or leaving
                address=tail[-3] if after >= 3 else "",
            )
        )
    return rows


def cvar_value(answer: str, name: str) -> str | None:
    """The value of a cvar from the console's answer to its name."""
    for match in CVAR_VALUE.finditer(answer):
        if match["name"].lower() == name.lower():
            value = match["value"]
            return value[:-2] if value.endswith("^7") else value
    return None


def rotation_maps(rotation: str) -> list[str]:
    """Map names from a ``sv_mapRotation`` value: ``gametype war map mp_crash map ...``."""
    words = rotation.split()
    names: dict[str, None] = {}
    for word, following in itertools.pairwise(words):
        if word.lower() == "map" and MAP_NAME.fullmatch(following):
            names.setdefault(following)
    return list(names)


def directory_maps(listing: str, extension: str = "bsp") -> list[str]:
    """Map names from the answer to ``dir <folder> <extension>``, in the server's order."""
    names: dict[str, None] = {}
    for line in listing.splitlines():
        name, dot, found = line.strip().rpartition(".")
        if dot and found.lower() == extension and MAP_NAME.fullmatch(name):
            names.setdefault(name)
    return list(names)


def ip_of(address: str) -> str | None:
    """The IPv4 address in "10.0.0.5:27960"; ``None`` for "bot", "localhost" and the like."""
    host, _, _ = address.rpartition(":")
    try:
        return str(ipaddress.IPv4Address(host or address))
    except ValueError:
        return None


def safe_message(text: str) -> str:
    """A kick reason reduced to characters that cannot change the command."""
    return " ".join(UNSAFE_IN_MESSAGE.sub("", text).split())[:MAX_MESSAGE_LENGTH]


def _int(value: str) -> int | None:
    return int(value) if re.fullmatch(r"-?[0-9]{1,9}", value) else None


class Quake3FamilyAdapter(GameAdapter):
    """Everything is in here; the games set their attributes and ``_reported_maps()``."""

    family = "quake3"
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
            Capability.DISCOVER,
        }
    )
    connection_schema: ClassVar[type[ConnectionSettings]] = Quake3Connection
    #: g_gametype value -> display name. The first one is used when the status names none.
    game_types: ClassVar[Mapping[str, str]]
    #: Restarts: the console command, or None for "load the current map again".
    soft_restart: ClassVar[str | None] = "map_restart"
    hard_restart: ClassVar[str | None] = None
    #: Whether the kick command takes a reason after the slot (CoD4X).
    kick_takes_reason: ClassVar[bool] = False
    #: The password flag when getstatus names none: CoD4X sends "pswrd" only when it is 1.
    open_without_flag: ClassVar[str | None] = None
    #: Pause between RCON commands: Quake 3, CoD 1 and WaW drop commands less than 500 ms
    #: apart (measured: 550 ms was enough, 500 ms not always).
    rcon_interval_seconds: ClassVar[float] = 0.7
    #: How long to wait for more output after the last packet (there is no end marker).
    rcon_quiet_seconds: ClassVar[float] = 0.3

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, Quake3Connection)  # noqa: S101 - see base class
        self._host = connection.host
        self._port = connection.port
        self._password = connection.rcon_password.get_secret_value()
        self._rcon_client: Quake3Rcon | None = None

    def _rcon(self) -> Quake3Rcon:
        # Created on first use: an unusable password must not keep the status from working.
        if self._rcon_client is None:
            self._rcon_client = Quake3Rcon(
                self._host,
                self._port,
                self._password,
                quiet_seconds=self.rcon_quiet_seconds,
                min_interval_seconds=self.rcon_interval_seconds,
            )
        return self._rcon_client

    async def _execute(self, command: str, *, loads_level: bool = False) -> str:
        # A level load answers only once it is done (United Offensive: 2-3 s and more).
        wait = LEVEL_LOAD_SECONDS if loads_level else None
        return await self._rcon().execute(command, first_answer_seconds=wait)

    # --- status ------------------------------------------------------------------------

    async def query(self) -> ServerStatus:
        status = await get_status(self._host, self._port, QUERY_TIMEOUT_SECONDS)
        info = status.info
        map_name = info.get("mapname", "")
        code = info.get("g_gametype", "")
        needpass_flag = info.get("g_needpass", info.get("pswrd", self.open_without_flag))
        return ServerStatus(
            name=strip_colors(info.get("sv_hostname", "")).strip()[:MAX_NAME_LENGTH],
            players=len(status.players),
            max_players=_int(info.get("sv_maxclients", "")) or 0,
            map=MapRef(map_name) if MAP_NAME.fullmatch(map_name) else None,
            game_type=self._game_type_name(code) if code else None,
            password_protected=None if needpass_flag is None else needpass_flag == "1",
        )

    def _game_type_name(self, code: str) -> str:
        name = self.game_types.get(code.lower())
        if name:
            return name
        return strip_colors(code)[:MAX_NAME_LENGTH] if code.isprintable() else "?"

    # --- maps --------------------------------------------------------------------------

    async def _reported_maps(self) -> list[str]:
        raise NotImplementedError

    async def list_maps(self) -> Sequence[MapRef]:
        names = (await self._reported_maps())[:MAX_MAPS]
        return [
            MapRef(name, None, label, {GAME_TYPE_KEY: code})
            for code, label in self.game_types.items()
            for name in names
        ]

    async def change_map(self, target: MapRef) -> None:
        code = target.extra.get(GAME_TYPE_KEY)
        if not MAP_NAME.fullmatch(target.key) or (code is not None and code not in self.game_types):
            raise CommandRejectedError(M("This map name cannot be sent to the server."))
        if code is None:
            await self._load(target.key)
            return
        previous = (await get_status(self._host, self._port, QUERY_TIMEOUT_SECONDS)).info.get(
            "g_gametype", ""
        )
        # Latched: takes effect with the map command that follows. Also set when it is the
        # running one, which clears a value someone else left pending (not on CoD4X).
        await self._execute(f"g_gametype {code}")
        try:
            await self._load(target.key)
        except CommandRejectedError:
            if code != previous.lower() and GAME_TYPE_CODE.fullmatch(previous):
                # Not left for the next restart. CoD4X keeps it pending nonetheless.
                await self._execute(f"g_gametype {previous}")
            raise
        # The map command loads the level before it answers, so the status is current. A
        # map made without that game type falls back to another one (United Offensive's
        # mp_sicily and Base Assault became Deathmatch).
        running = (await get_status(self._host, self._port, QUERY_TIMEOUT_SECONDS)).info.get(
            "g_gametype", ""
        )
        if running and running.lower() != code:
            raise CommandRejectedError(
                M(
                    "The map {key} was loaded, but not as {wanted}: the server runs "
                    "{running}, probably because the map is not made for that game type.",
                    key=target.key,
                    wanted=self.game_types[code],
                    running=self._game_type_name(running),
                )
            )

    async def _load(self, name: str) -> None:
        answer = await self._execute(f"map {name}", loads_level=True)
        if MISSING_MAP in answer:
            raise CommandRejectedError(M("The server does not have the map {key}.", key=name))

    async def restart(self, mode: RestartMode) -> None:
        command = self.soft_restart if mode is RestartMode.SOFT else self.hard_restart
        if command is not None:
            await self._execute(command, loads_level=True)
            return
        current = (await self.query()).map
        if current is None:
            raise CommandRejectedError(M("The server did not report its current map."))
        await self._load(current.key)

    # --- players -----------------------------------------------------------------------

    async def _entries(self) -> list[StatusEntry]:
        return parse_status(await self._execute("status"))

    def _player(self, entry: StatusEntry) -> Player:
        bot = entry.address.lower() == "bot"
        return Player(
            id=entry.slot,
            name=entry.name,
            score=entry.score,
            ping=None if bot else entry.ping,
            bot=bot,
            can_ban=Capability.BAN in self.capabilities and ip_of(entry.address) is not None,
        )

    async def list_players(self) -> Sequence[Player]:
        return [self._player(entry) for entry in await self._entries()]

    async def _still_there(self, player: Player) -> StatusEntry:
        """The status row of ``player``. Slots are reused when players leave, so the name
        must still match before a slot number goes into a command."""
        if not SLOT.fullmatch(player.id):
            raise CommandRejectedError(M("This player cannot be named in a command."))
        for entry in await self._entries():
            if entry.slot == player.id and entry.name == player.name:
                return entry
        raise CommandRejectedError(M("The player is no longer on the server."))

    def _kick_command(self, slot: str, reason: str) -> str:
        message = safe_message(reason) if self.kick_takes_reason else ""
        return f"clientkick {slot} {message}".rstrip()

    async def kick(self, player: Player, reason: str = "") -> None:
        entry = await self._still_there(player)
        await self._execute(self._kick_command(entry.slot, reason))

    async def console(self, command: str) -> str:
        return await self._execute(command)

    # --- LAN scan ----------------------------------------------------------------------

    @classmethod
    def _is_mine(cls, info: Mapping[str, str]) -> bool:
        raise NotImplementedError

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        found = []
        for host, port, info in await find_servers(networks, wait):
            if cls._is_mine(info):
                found.append(
                    DiscoveredServer(
                        cls.id,
                        host,
                        port,
                        name=discovery.clean_text(strip_colors(info.get("sv_hostname", ""))),
                        game_type=discovery.clean_text(
                            cls.game_types.get(info.get("g_gametype", "").lower(), "")
                        ),
                    )
                )
        return found


class IpBanAdapter(Quake3FamilyAdapter):
    """For engines whose game code filters addresses: ``BAN`` is ``addip`` and a kick. The
    ban lives in the ``g_banIPs`` cvar, so it lasts until the server is restarted."""

    capabilities = Quake3FamilyAdapter.capabilities | {Capability.BAN}

    async def ban(self, player: Player, reason: str = "") -> None:
        entry = await self._still_there(player)
        address = ip_of(entry.address)
        if address is None:
            raise CommandRejectedError(
                M("The server did not report this player's IP address, so it cannot ban it.")
            )
        await self._execute(f"addip {address}")
        await self._execute(self._kick_command(entry.slot, reason))


# --- discovery: one getstatus broadcast for all games of the family ----------------------

# The in-game LAN browsers ask the first four ports from the default one.
SCAN_PORTS = (*range(27960, 27964), *range(28960, 28964))
_scans: dict[
    tuple[tuple[IPv4Network, ...], float],
    asyncio.Task[list[tuple[str, int, dict[str, str]]]],
] = {}


async def find_servers(
    networks: Sequence[IPv4Network], wait: float
) -> list[tuple[str, int, dict[str, str]]]:
    """Every server of the family that answered: host, port and its info string. The
    adapters of one scan ask together, so they share one broadcast."""
    key = (tuple(networks), wait)
    task = _scans.get(key)
    if task is None:
        task = asyncio.ensure_future(_find_servers(networks, wait))
        _scans[key] = task
        task.add_done_callback(lambda _: _scans.pop(key, None))
    return await asyncio.shield(task)


async def _find_servers(
    networks: Sequence[IPv4Network], wait: float
) -> list[tuple[str, int, dict[str, str]]]:
    packets = [(HEADER + b"getstatus\n", port) for port in SCAN_PORTS]
    found: dict[tuple[str, int], dict[str, str]] = {}
    for reply in await discovery.broadcast(networks, packets, wait):
        try:
            status = parse_status_response(reply.data)
        except ProtocolError:
            continue  # not a status answer
        found.setdefault((reply.host, reply.port), status.info)
    return [(host, port, info) for (host, port), info in found.items()]
