"""Counter-Strike 1.6 (GoldSrc engine, Half-Life Dedicated Server).

Status comes from the server query, which needs no password; everything else goes through
RCON. Protocol facts and how they were measured: docs/games/counter-strike.md.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

from opengsq.protocols.source import Source
from opengsq.responses.source import Visibility
from pydantic import Field, SecretStr

from lanpilot.core.adapters import (
    AdapterError,
    Capability,
    CommandRejectedError,
    ConnectionSettings,
    GameAdapter,
    MapRef,
    ProtocolError,
    RestartMode,
    ServerStatus,
    ServerUnreachableError,
)
from lanpilot.core.transports.goldsrc_rcon import GoldSrcRcon

# Map names as they occur in practice. Everything that goes into a console command is
# checked against this, so a crafted name cannot carry a second command.
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
QUERY_TIMEOUT_SECONDS = 3.0
# "stats" needs RCON and shows up in the server's log, so it is not asked with every poll.
STATS_INTERVAL_SECONDS = 60.0
MAX_NAME_LENGTH = 100


class CounterStrike16Connection(ConnectionSettings):
    port: int = Field(default=27015, title="Port", ge=1, le=65535)
    rcon_password: SecretStr = Field(
        title="RCON password",
        description="The value of rcon_password in the server's configuration (server.cfg).",
    )


class CounterStrike16Adapter(GameAdapter):
    id = "cs16"
    name = "Counter-Strike 1.6"
    family = "counterstrike"
    capabilities = frozenset(
        {
            Capability.STATUS,
            Capability.MAP_LIST,
            Capability.MAP_CHANGE,
            Capability.RESTART_SOFT,
            Capability.RESTART_HARD,
            Capability.LOAD_STATS,
        }
    )
    connection_schema = CounterStrike16Connection

    def __init__(self, connection: ConnectionSettings) -> None:
        super().__init__(connection)
        assert isinstance(connection, CounterStrike16Connection)  # noqa: S101 - see base class
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
        try:
            info = await Source(self._host, self._port, QUERY_TIMEOUT_SECONDS).get_info()
        except (TimeoutError, OSError) as exc:
            raise ServerUnreachableError("The server did not answer the status query.") from exc
        except Exception as exc:
            raise ProtocolError("The server's status answer could not be read.") from exc

        current = str(info.map)
        return ServerStatus(
            name=str(info.name)[:MAX_NAME_LENGTH],
            players=int(info.players),
            max_players=int(info.max_players),
            map=MapRef(current) if MAP_NAME.fullmatch(current) else None,
            password_protected=info.visibility == Visibility.Private,
            load=await self._current_load(),
        )

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
            raise CommandRejectedError("This map name cannot be sent to the server.")
        answer = await self._rcon().execute(f"changelevel {target.key}")
        if "changelevel failed" in answer:
            raise CommandRejectedError(f"The server does not have the map {target.key}.")

    async def restart(self, mode: RestartMode) -> None:
        # sv_restart restarts the round after the given number of seconds and resets the
        # scores; "restart" loads the current map again.
        await self._rcon().execute("sv_restart 1" if mode is RestartMode.SOFT else "restart")


def parse_maps(listing: str) -> list[str]:
    """Map names from the answer to ``maps *``, without duplicates, in the server's order."""
    names: dict[str, None] = {}
    for line in listing.splitlines():
        name, dot, extension = line.strip().rpartition(".")
        if dot and extension.lower() == "bsp" and MAP_NAME.fullmatch(name):
            names.setdefault(name)
    return list(names)


def parse_stats(answer: str) -> dict[str, float]:
    """CPU load and server frame rate from the answer to ``stats``."""
    lines = [line.split() for line in answer.splitlines() if line.strip()]
    if len(lines) < 2:
        return {}
    values = dict(zip((column.lower() for column in lines[0]), lines[1], strict=False))
    load: dict[str, float] = {}
    for column in ("cpu", "fps"):
        try:
            load[column] = float(values[column])
        except (KeyError, ValueError):
            continue
    return load
