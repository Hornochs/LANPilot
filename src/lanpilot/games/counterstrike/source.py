"""Counter-Strike: Source, CS:GO and CS2 (Source and Source 2 engines, Source RCON).

Status comes from the server query, which needs no password; everything else goes through
RCON over TCP. Protocol facts and how they were measured: docs/games/counter-strike.md.
CS2 and CS:Source have been measured; CS:GO is assumed to behave like CS:Source.

Player control (CS:Source, CS:GO) reads ``status`` and kicks with ``kickid``. CS2 prints
its players in another format, which has not been measured, so it offers only the console.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Mapping, Sequence
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
    ProtocolError,
    RestartMode,
    ServerStatus,
)
from lanpilot.core.transports.source_rcon import SourceRcon
from lanpilot.games.counterstrike.common import (
    MAP_NAME,
    MAX_NAME_LENGTH,
    STATS_INTERVAL_SECONDS,
    RconPlayerControl,
    ip_of,
    parse_stats,
    query_status,
)

# Answers to "changelevel" for a map the server does not have: CS2, CS:Source, CS:GO.
UNKNOWN_MAP_ANSWERS = ("invalid map name", "Failed to find map", "changelevel failed")
# CS2 drops a change that arrives while it is still loading the previous map.
LOADING_ANSWER = "server not in a map"
# A map in the Source 1 answer to "maps *": "PENDING:   (fs) de_dust2" on CS:Source,
# "PENDING:   (fs) de_dust2.bsp" on CS:GO.
SOURCE1_MAP_LINE = re.compile(r"\S+:\s+\(\w+\)\s+(\S+?)(?:\.bsp)?", re.IGNORECASE)
# Resources that "maps *" lists next to the playable maps (CS2; CS:GO has lobby_mapveto).
NON_MAPS = frozenset({"error", "graphics_settings", "lobby_mapveto"})
WORKSHOP = "workshop"
# A player in the answer to "status":
#   CS:Source  #      3 "Ada" STEAM_0:1:23 01:23 25 0 active 10.0.0.5:27005
#   CS:GO      #  3 2 "Ada" STEAM_1:1:23 01:23 25 0 active 196608 10.0.0.5:27005
#   bots       #      2 "Bot01" BOT active  (CS:GO: #  2 1 "Bot01" BOT active 64)
# user id (CS:GO: then the slot), quoted name (matched greedily), unique id, the rest.
STATUS_PLAYER = re.compile(
    r'#\s*(?P<userid>[0-9]{1,9})\s+(?:[0-9]{1,4}\s+)?"(?P<name>.*)"\s+(?P<uniqueid>\S+)'
    r"(?P<rest>(?:\s+\S+)*)\s*"
)
CONNECTED = re.compile(r"[0-9]{1,4}(?::[0-9]{2}){1,2}")


class SourceConnection(ConnectionSettings):
    port: int = Field(
        default=27015,
        title="Port",
        description="The game port. RCON uses the same port over TCP.",
        ge=1,
        le=65535,
    )
    rcon_password: SecretStr = Field(
        title="RCON password",
        description="The value of rcon_password in the server's configuration (server.cfg).",
    )


class SourceCounterStrikeAdapter(GameAdapter):
    """What CS:Source, CS:GO and CS2 have in common. Not registered itself."""

    family = "counterstrike"
    connection_schema = SourceConnection
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.MAP_CHANGE,
            Capability.RESTART_SOFT,
            Capability.RESTART_HARD,
            Capability.CONSOLE,
        }
    )
    #: Whether ``maps *`` answers in the CS2 format (bare names) instead of the Source 1 one.
    cs2_listing: ClassVar[bool] = False

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, SourceConnection)  # noqa: S101 - see base class
        self._host = connection.host
        self._port = connection.port
        self._password = connection.rcon_password.get_secret_value()
        self._rcon_client: SourceRcon | None = None
        self._load: dict[str, float] = {}
        self._load_checked_at: float | None = None

    def _rcon(self) -> SourceRcon:
        # Created on first use: an unusable password must not keep the status from working.
        if self._rcon_client is None:
            self._rcon_client = SourceRcon(self._host, self._port, self._password)
        return self._rcon_client

    async def close(self) -> None:
        if self._rcon_client is not None:
            await self._rcon_client.close()

    async def query(self) -> ServerStatus:
        status = await query_status(self._host, self._port)
        if self.supports(Capability.LOAD_STATS):
            status = dataclasses.replace(status, load=await self._current_load())
        return status

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
        listing = await self._rcon().execute("maps *")
        return [MapRef(name) for name in parse_maps(listing, cs2=self.cs2_listing)]

    async def change_map(self, target: MapRef) -> None:
        await self._change_level(target.key, workshop=target.extra.get(WORKSHOP) == "1")

    async def _change_level(self, name: str, *, workshop: bool = False) -> None:
        if not MAP_NAME.fullmatch(name):
            raise CommandRejectedError("This map name cannot be sent to the server.")
        command = "ds_workshop_changelevel" if workshop else "changelevel"
        answer = await self._rcon().execute(f"{command} {name}")
        if any(text in answer for text in UNKNOWN_MAP_ANSWERS):
            raise CommandRejectedError(f"The server does not have the map {name}.")
        if LOADING_ANSWER in answer:
            raise CommandRejectedError(
                "The server is still loading a map and ignored the change. "
                "Try again in a few seconds."
            )

    async def restart(self, mode: RestartMode) -> None:
        if mode is RestartMode.SOFT:
            # Restarts the match after one second; scores are reset, players stay.
            await self._rcon().execute("mp_restartgame 1")
            return
        # "restart" is a cheat command on CS2, so the level is reloaded by changing to it.
        current = (await query_status(self._host, self._port)).map
        if current is None:
            raise ProtocolError("The server did not report which map is running.")
        await self._change_level(current.key, workshop=await self._is_workshop_map(current.key))

    async def _is_workshop_map(self, name: str) -> bool:
        return False

    async def console(self, command: str) -> str:
        return await self._rcon().execute(command)


class Source1PlayerControl(RconPlayerControl, SourceCounterStrikeAdapter):
    """Players over RCON for the Source 1 games. Not registered itself."""

    capabilities = SourceCounterStrikeAdapter.capabilities | {
        Capability.LOAD_STATS,
        Capability.PLAYERS,
        Capability.KICK,
        Capability.BAN,
    }

    async def _execute(self, command: str) -> str:
        return await self._rcon().execute(command)

    def _parse_status(self, answer: str) -> list[tuple[Player, str | None]]:
        return parse_status(answer)

    def _kick_command(self, user_id: str, message: str) -> str:
        return f"kickid {user_id} {message}".rstrip()


class CounterStrikeSourceAdapter(Source1PlayerControl):
    id = "css"
    name = "Counter-Strike: Source"
    image_lookup: ClassVar[Mapping[str, str]] = {"gametracker": "css", "gamebanana": "2"}


class CounterStrikeGlobalOffensiveAdapter(Source1PlayerControl):
    id = "csgo"
    name = "Counter-Strike: Global Offensive"
    image_lookup: ClassVar[Mapping[str, str]] = {
        "gametracker": "csgo",
        "gamebanana": "4660",
        "steam_app": "730",
    }


class CounterStrike2Adapter(SourceCounterStrikeAdapter):
    """CS2 prints nothing for ``stats``, so it has no load values."""

    id = "cs2"
    name = "Counter-Strike 2"
    # GameTracker has no CS2 section; its CS:GO pictures show the same layouts.
    image_lookup: ClassVar[Mapping[str, str]] = {
        "gametracker": "csgo",
        "gamebanana": "18134",
        "steam_app": "730",
    }
    cs2_listing = True

    async def list_maps(self) -> Sequence[MapRef]:
        stock = await super().list_maps()
        workshop = parse_workshop_maps(await self._rcon().execute("ds_workshop_listmaps"))
        # A downloaded workshop map shows up in "maps *" too, but only
        # ds_workshop_changelevel can load it, so the workshop entry wins.
        return [
            *(item for item in stock if item.key not in workshop),
            *(MapRef(name, extra={WORKSHOP: "1"}) for name in workshop),
        ]

    async def _is_workshop_map(self, name: str) -> bool:
        listing = await self._rcon().execute("ds_workshop_listmaps")
        return name in parse_workshop_maps(listing)


def parse_status(answer: str) -> list[tuple[Player, str | None]]:
    """Players and their IP addresses from a Source 1 answer to ``status``. It has no
    scores."""
    players = []
    for line in answer.splitlines():
        match = STATUS_PLAYER.fullmatch(line.strip())
        if match is None:
            continue
        rest = match["rest"].split()
        bot = match["uniqueid"] == "BOT"
        # Players: connected time, ping, loss, state, (CS:GO: rate,) address.
        human = len(rest) >= 5 and CONNECTED.fullmatch(rest[0]) is not None
        address = ip_of(rest[-1]) if human and not bot else None
        player = Player(
            id=match["userid"],
            name=match["name"][:MAX_NAME_LENGTH],
            ping=int(rest[1]) if human and rest[1].isdecimal() and len(rest[1]) < 6 else None,
            bot=bot,
            can_ban=address is not None,
        )
        players.append((player, address))
    return players


def parse_maps(listing: str, *, cs2: bool) -> list[str]:
    """Map names from the answer to ``maps *``, without duplicates, in the server's order.

    Source 1 prints ``PENDING:   (fs) de_dust2`` (CS:GO: ``de_dust2.bsp``) per map. CS2
    prints one bare name per line and lists other resources too (``prefabs/…``, ``ui/…``,
    ``*_vanity`` scenes, workshop previews); those are left out.
    """
    names: dict[str, None] = {}
    for line in listing.splitlines():
        if cs2:
            name = line.strip()
            if _cs2_resource(name):
                continue
        else:
            match = SOURCE1_MAP_LINE.fullmatch(line.strip())
            if match is None:
                continue
            name = match.group(1)
            if name in NON_MAPS:
                continue
        if MAP_NAME.fullmatch(name):
            names.setdefault(name)
    return list(names)


def _cs2_resource(name: str) -> bool:
    return name in NON_MAPS or name.endswith("_vanity") or name.startswith("workshop_preview_")


def parse_workshop_maps(listing: str) -> list[str]:
    """Map names from the answer to ``ds_workshop_listmaps``: one name per line."""
    names: dict[str, None] = {}
    for line in listing.splitlines():
        name = line.strip()
        if MAP_NAME.fullmatch(name):
            names.setdefault(name)
    return list(names)
