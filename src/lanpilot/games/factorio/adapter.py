"""Factorio (headless server, Source RCON).

Everything goes through RCON over TCP. Factorio answers every command with exactly one
packet and ignores empty ones, so the transport runs in single-packet mode. Protocol facts
and how they were measured: docs/games/factorio.md.

**Achievements.** A save loses its achievements for good as soon as a Lua command runs on
it (``/c`` and its relatives), or the map editor or cheat mode is used. LANPilot never sends
such a command, and the console refuses them. Lines without a leading ``/`` are refused
too: Factorio would post them to every player as a chat message.

There are no maps to change: a Factorio "map" is a save file, and RCON cannot load another
one. Restarting (``/server-save``, then ``/quit``) only brings the game back when the
server's service manager starts it again, so it has to be switched on per server.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Sequence
from ipaddress import IPv4Network

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
    ServerUnreachableError,
    answer_lines,
)
from lanpilot.core.transports.source_rcon import SourceRcon
from lanpilot.games.factorio import lan
from lanpilot.games.factorio.lan import GAME_PORT, format_game_type
from lanpilot.i18n import M, Message

STATS_INTERVAL_SECONDS = 60.0
RCON_TIMEOUT_SECONDS = 5.0
# A restart has to fit into LANPilot's limit for an action (15 s), /quit included.
SAVE_TIMEOUT_SECONDS = 10.0
# An answer to /server-save that reports a problem (not measured: Factorio's wording of a
# failed save is unknown, so this errs on the side of not stopping the server).
SAVE_FAILED = re.compile(r"error|fail|cannot|can't|unable", re.IGNORECASE)
LAN_INFO_WAIT_SECONDS = 1.0
MAX_DETAIL_LENGTH = 3000
MAX_LIST_ITEMS = 30
# A planet in the answer to /evolution; surfaces that mods create carry no such tag.
PLANET_TAG = re.compile(r"^\[planet=([a-z0-9_-]{1,40})\]", re.MULTILINE)
PLAYER_COUNT = re.compile(r"Players \(([0-9]{1,9})\):")
AUTOSAVE = re.compile(r"every ([0-9]{1,6}) minute")
VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,5}")
# Factorio user names: letters, digits, "-", "_" and ".".
PLAYER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,60}")
# A suffix such as " (online)" or " (admin)" after a name in "/players".
NAME_SUFFIX = re.compile(r"\s+\([^()]*\)$")
UNSAFE_IN_REASON = re.compile(r"[^A-Za-z0-9 .,!?_-]")
MAX_REASON_LENGTH = 100
DEFAULT_REASON = "by LANPilot"
SPACE_AGE_PLANETS = ("vulcanus", "fulgora", "gleba", "aquilo")
SPACE_AGE = "Space Age"
# The "map" is the edition; its Steam app gives the picture (image source steam_store).
EDITIONS = {False: ("factorio", "427520"), True: ("space-age", "645390")}
SEED = re.compile(r"[0-9]{1,20}")
TIME_PART = re.compile(r"(\d+)\s+(day|hour|minute|second)s?\b")
TIME_UNITS = {"day": 24.0, "hour": 1.0, "minute": 1 / 60, "second": 1 / 3600}
# Commands that turn achievements off or break the game; refused in the console.
FORBIDDEN_COMMANDS = frozenset(
    {
        "/c",
        "/command",
        "/sc",
        "/silent-command",
        "/mc",
        "/measured-command",
        "/editor",
        "/cheat",
        "/toggle-heavy-mode",
        "/delete-blueprint-library",
    }
)


class FactorioConnection(ConnectionSettings):
    port: int = Field(default=27015, title=M("RCON port"), ge=1, le=65535)
    password: SecretStr = Field(
        title=M("RCON password"),
        description=M("The value of --rcon-password on the server's command line."),
    )
    game_port: int = Field(default=GAME_PORT, title=M("Game port"), ge=1, le=65535)
    restarts_itself: bool = Field(
        default=False,
        title=M("The server is started again automatically after /quit"),
        description=M(
            "For example systemd with Restart=always and --start-server-load-latest. "
            "Only then may LANPilot restart it."
        ),
    )


def parse_time(text: str) -> float | None:
    """Hours from the answer to ``/time`` ("2413 hours and 1 minute"); ``None`` if unknown."""
    parts = TIME_PART.findall(text)
    if not parts:
        return None
    return sum(int(value) * TIME_UNITS[unit] for value, unit in parts)


def parse_players(text: str) -> list[Player]:
    """Players in the answer to ``/players online``; odd names are left out."""
    players = []
    for line in answer_lines(text)[1:]:
        name = line.strip()
        while NAME_SUFFIX.search(name):
            name = NAME_SUFFIX.sub("", name)
        if PLAYER_NAME.fullmatch(name):
            players.append(Player(id=name, name=name))
    return players


def clean_reason(text: str) -> str:
    """A kick or ban reason reduced to characters that cannot change the command."""
    reason = " ".join(UNSAFE_IN_REASON.sub("", text).split())[:MAX_REASON_LENGTH].strip()
    return reason or DEFAULT_REASON


def format_list(items: Sequence[str]) -> str:
    """One item per line; a long list ends with how many items were left out."""
    if len(items) <= MAX_LIST_ITEMS:
        return "\n".join(items)
    return M(
        "{items}\n… and {count} more",
        items="\n".join(items[:MAX_LIST_ITEMS]),
        count=len(items) - MAX_LIST_ITEMS,
    )


def check_console_command(command: str) -> str:
    """Return ``command`` without surrounding whitespace, or refuse it."""
    line = command.strip()
    if not line.startswith("/"):
        raise CommandRejectedError(
            M("Factorio commands start with /. Other lines would be sent to the chat.")
        )
    if line.split()[0].lower() == "/quit":
        raise CommandRejectedError(
            M(
                "LANPilot does not stop Factorio servers from the console. Use Restart, "
                "which is offered only for servers that are started again automatically."
            )
        )
    if line.split()[0].lower() in FORBIDDEN_COMMANDS:
        raise CommandRejectedError(
            M(
                "LANPilot does not send this command: it would turn off achievements "
                "or damage the game."
            )
        )
    return line


class FactorioAdapter(GameAdapter):
    id = "factorio"
    name = "Factorio"
    family = "factorio"
    connection_schema = FactorioConnection
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.PLAYERS,
            Capability.KICK,
            Capability.BAN,
            Capability.CONSOLE,
            Capability.RESTART_HARD,
            Capability.DISCOVER,
        }
    )

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, FactorioConnection)  # noqa: S101 - see base class
        self._settings = connection
        self._rcon_client: SourceRcon | None = None
        self._game_type: str | None = None
        self._map: MapRef | None = None
        self._details: dict[str, str] = {}
        self._stats_checked_at: float | None = None

    def _rcon(self) -> SourceRcon:
        if self._rcon_client is None:
            self._rcon_client = SourceRcon(
                self._settings.host,
                self._settings.port,
                self._settings.password.get_secret_value(),
                timeout_seconds=RCON_TIMEOUT_SECONDS,
                single_packet_answers=True,
            )
        return self._rcon_client

    async def _execute(self, command: str) -> str:
        return await self._rcon().execute(command)

    async def close(self) -> None:
        if self._rcon_client is not None:
            await self._rcon_client.close()

    async def query(self) -> ServerStatus:
        name = (await self._execute("/config get name")).strip()
        name = name.removeprefix("Server name:").strip()
        try:
            max_players = int((await self._execute("/config get max-players")).strip())
        except ValueError as exc:
            raise ProtocolError(M("The server's answer has an unexpected format.")) from exc
        players = parse_players(await self._execute("/players online"))
        await self._refresh_stats()
        return ServerStatus(
            name=name[:100],
            players=len(players),
            max_players=max(max_players, 0),
            map=self._map,
            game_type=self._game_type,
            details=dict(self._details),
        )

    async def _refresh_stats(self) -> None:
        now = time.monotonic()
        if self._stats_checked_at is not None and (
            now - self._stats_checked_at < STATS_INTERVAL_SECONDS
        ):
            return
        # The mod list in the LAN info is authoritative; /evolution only lists planets that
        # were reached, so a new Space Age map shows Nauvis alone.
        info = await lan.query_info(
            self._settings.host, self._settings.game_port, LAN_INFO_WAIT_SECONDS
        )
        evolution = (await self._execute("/evolution")).lower()
        planets = list(dict.fromkeys(PLANET_TAG.findall(evolution)))
        reached_space_age = bool(set(planets) & set(SPACE_AGE_PLANETS))
        space_age = info.space_age if info is not None else reached_space_age
        self._game_type = SPACE_AGE if space_age else None
        seed = (await self._execute("/seed")).strip()
        key, app = EDITIONS[space_age]
        self._map = (
            MapRef(key, name=f"World {seed}", extra={"steam_store_app": app})
            if SEED.fullmatch(seed)
            else None
        )
        details: dict[str, str] = {}
        version = (await self._execute("/version")).strip()
        if VERSION.fullmatch(version):
            details[M("Version")] = version
        details[M("Planets")] = format_list([planet.capitalize() for planet in planets])
        if info is not None:
            details[M("Mods")] = format_list(
                [f"{name} {mod_version}" for name, mod_version in info.mods if name != "base"]
            )
        hours = parse_time(await self._execute("/time"))
        if hours is not None:
            whole, minutes = divmod(round(hours * 60), 60)
            details[M("Map age")] = M("{hours} h {minutes} min", hours=whole, minutes=minutes)
        players = PLAYER_COUNT.match((await self._execute("/players count")).strip())
        if players:
            details[M("Players ever joined")] = players.group(1)
        autosave = AUTOSAVE.search(await self._execute("/config get autosave-interval"))
        if autosave:
            details[M("Autosave")] = M("every {minutes} min", minutes=autosave.group(1))
        self._details = {
            name: value if isinstance(value, Message) else value[:MAX_DETAIL_LENGTH]
            for name, value in details.items()
            if value
        }
        # Only now: a refresh that failed half-way is tried again at the next poll.
        self._stats_checked_at = now

    async def list_maps(self) -> Sequence[MapRef]:
        """The edition, as the only map: it carries the picture of the server's card."""
        await self._refresh_stats()
        return [self._map] if self._map is not None else []

    async def list_players(self) -> Sequence[Player]:
        return parse_players(await self._execute("/players online"))

    async def kick(self, player: Player, reason: str = "") -> None:
        await self._remove("/kick", player, reason)

    async def ban(self, player: Player, reason: str = "") -> None:
        await self._remove("/ban", player, reason)

    async def _remove(self, command: str, player: Player, reason: str) -> None:
        if not PLAYER_NAME.fullmatch(player.id):
            raise CommandRejectedError(M("This player name cannot be sent to the server."))
        answer = await self._execute(f"{command} {player.id} {clean_reason(reason)}")
        if "doesn't exist" in answer:
            raise CommandRejectedError(M("No player named {name} is online.", name=player.id))

    async def restart(self, mode: RestartMode) -> None:
        if mode is not RestartMode.HARD:
            await super().restart(mode)
        if not self._settings.restarts_itself:
            raise CommandRejectedError(
                M(
                    "Restarting is switched off for this server. Enable it in the server's "
                    "settings if the server is started again automatically after /quit."
                )
            )
        # Connections of their own: saving gets a longer timeout than other commands, and a
        # fresh connection is never retried, so /quit is sent exactly once.
        try:
            save = self._restart_rcon()
            try:
                answer = await save.execute("/server-save")
            except ServerUnreachableError as exc:
                raise CommandRejectedError(
                    M(
                        "Saving the map took longer than {seconds} seconds, so the server was "
                        "not stopped.",
                        seconds=f"{SAVE_TIMEOUT_SECONDS:.0f}",
                    )
                ) from exc
            finally:
                await save.close()
            if SAVE_FAILED.search(answer):
                raise CommandRejectedError(
                    M(
                        "The server could not save the map, so it was not stopped: {answer}",
                        answer=" ".join(answer.split())[:200],
                    )
                )
            quit_ = self._restart_rcon()
            try:
                with contextlib.suppress(ServerUnreachableError):
                    # The server may close the connection without answering: it is quitting.
                    await quit_.execute("/quit")
            finally:
                await quit_.close()
        finally:
            await self.close()

    def _restart_rcon(self) -> SourceRcon:
        return SourceRcon(
            self._settings.host,
            self._settings.port,
            self._settings.password.get_secret_value(),
            timeout_seconds=SAVE_TIMEOUT_SECONDS,
            single_packet_answers=True,
        )

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        announced = await lan.listen_for_announcements(networks, wait)

        async def describe(host: str, game_port: int) -> DiscoveredServer:
            info = await lan.query_info(host, game_port)
            return DiscoveredServer(
                cls.id,
                host,
                port=None,  # the RCON port is not announced; the form keeps its default
                name=info.name if info and info.name else None,
                game_type=format_game_type(info.version, info.space_age) if info else None,
                values={"game_port": str(game_port)},
            )

        return await discovery.gather_found(
            describe(host, game_port) for host, game_port in announced.items()
        )

    async def console(self, command: str) -> str:
        return await self._execute(check_console_command(command))
