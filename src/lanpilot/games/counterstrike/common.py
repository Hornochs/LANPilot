"""What all Counter-Strike adapters share: the status query, map name checks, ``stats``,
and player control over RCON."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import Sequence
from ipaddress import IPv4Network
from typing import ClassVar

from opengsq.protocols.source import Source
from opengsq.responses.source import Visibility

from lanpilot.core import discovery
from lanpilot.core.adapters import (
    CommandRejectedError,
    DiscoveredServer,
    MapRef,
    Player,
    ProtocolError,
    ServerStatus,
    ServerUnreachableError,
)

# Map names as they occur in practice. Everything that goes into a console command is
# checked against this, so a crafted name cannot carry a second command.
MAP_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
QUERY_TIMEOUT_SECONDS = 3.0
# "stats" needs RCON and shows up in the server's log, so it is not asked with every poll.
STATS_INTERVAL_SECONDS = 60.0
MAX_NAME_LENGTH = 100
# What a kick message may contain: it becomes part of a console command.
UNSAFE_IN_MESSAGE = re.compile(r"[^A-Za-z0-9 .,!?()_-]")
MAX_MESSAGE_LENGTH = 64
USER_ID = re.compile(r"[0-9]{1,9}")


async def query_status(host: str, port: int) -> ServerStatus:
    """Status from the server query (A2S_INFO), which needs no password.

    A wrong remote-control password therefore never makes a server look offline.
    """
    try:
        info = await Source(host, port, QUERY_TIMEOUT_SECONDS).get_info()
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
    )


# Discovery: A2S_INFO broadcast to the usual query ports. Servers answer with a challenge
# (or, over loopback, the info right away); the info is then asked from each of them.
QUERY_PORTS = (*range(27015, 27021), *range(26900, 26906))
A2S_INFO = b"\xff\xff\xff\xffTSource Engine Query\x00"
A2S_ANSWERS = (b"\xff\xff\xff\xffA", b"\xff\xff\xff\xffI", b"\xff\xff\xff\xffm")
APP_CS16, APP_CSS, APP_CSGO = 10, 240, 730  # CS2 kept CS:GO's app id
FIRST_CS2_VERSION = (1, 39)  # CS:GO ended with 1.38.x
_scans: dict[tuple[tuple[IPv4Network, ...], float], asyncio.Task[list[DiscoveredServer]]] = {}


def classify(info: object) -> str | None:
    """The adapter id for a server's A2S_INFO answer, ``None`` for other games."""
    app = int(getattr(info, "id", 0) or 0)
    folder = str(getattr(info, "folder", "")).lower()
    if app == APP_CS16 and folder == "cstrike":
        return "cs16"
    if app == APP_CSS:
        return "css"
    if app != APP_CSGO:
        return None
    if str(getattr(info, "game", "")) == "Counter-Strike 2":
        return "cs2"
    version = re.match(r"(\d{1,3})\.(\d{1,3})", str(getattr(info, "version", "")))
    if version and (int(version[1]), int(version[2])) >= FIRST_CS2_VERSION:
        return "cs2"
    return "csgo"


async def find_servers(networks: Sequence[IPv4Network], wait: float) -> list[DiscoveredServer]:
    """Counter-Strike servers of all four games. The adapters of one scan ask together, so
    they share one broadcast."""
    key = (tuple(networks), wait)
    task = _scans.get(key)
    if task is None:
        task = asyncio.ensure_future(_find_servers(networks, wait))
        _scans[key] = task
        task.add_done_callback(lambda _: _scans.pop(key, None))
    return await asyncio.shield(task)


async def _find_servers(networks: Sequence[IPv4Network], wait: float) -> list[DiscoveredServer]:
    replies = await discovery.broadcast(networks, [(A2S_INFO, port) for port in QUERY_PORTS], wait)
    senders = {(r.host, r.port): None for r in replies if r.data.startswith(A2S_ANSWERS)}

    async def detail(host: str, port: int) -> DiscoveredServer | None:
        info = await Source(host, port, QUERY_TIMEOUT_SECONDS).get_info()
        adapter_id = classify(info)
        if adapter_id is None:
            return None
        return DiscoveredServer(adapter_id, host, port, name=discovery.clean_text(info.name))

    return await discovery.gather_found(detail(host, port) for host, port in senders)


class DiscoverCounterStrike:
    """``discover()`` for a Counter-Strike adapter: its own share of :func:`find_servers`."""

    id: ClassVar[str]

    @classmethod
    async def discover(
        cls, networks: Sequence[IPv4Network], wait: float
    ) -> Sequence[DiscoveredServer]:
        return [item for item in await find_servers(networks, wait) if item.adapter_id == cls.id]


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


def safe_message(text: str) -> str:
    """A kick message reduced to characters that cannot change the command."""
    return " ".join(UNSAFE_IN_MESSAGE.sub("", text).split())[:MAX_MESSAGE_LENGTH]


def ip_of(address: str | None) -> str | None:
    """The IPv4 address in "10.0.0.5:27005"; ``None`` for bots, "loopback" and the like."""
    host, _, _ = (address or "").rpartition(":")
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return None


class RconPlayerControl:
    """Players from ``status``, kick by user id, ban by IP address (``addip``, then the kick,
    then ``writeip`` so the ban survives a restart; server.cfg usually runs listip.cfg).

    A ban by Steam ID would not work at a LAN party: servers with ``sv_lan 1`` give every
    player the same ID (STEAM_ID_LAN).
    """

    def _parse_status(self, answer: str) -> list[tuple[Player, str | None]]:
        """Players and their IP addresses from the answer to ``status``."""
        raise NotImplementedError

    def _kick_command(self, user_id: str, message: str) -> str:
        raise NotImplementedError

    async def _execute(self, command: str) -> str:
        raise NotImplementedError

    async def list_players(self) -> Sequence[Player]:
        return [player for player, _ in self._parse_status(await self._execute("status"))]

    async def kick(self, player: Player, reason: str = "") -> None:
        await self._execute(self._kick_command(_user_id(player), safe_message(reason)))

    async def ban(self, player: Player, reason: str = "") -> None:
        user_id = _user_id(player)
        entries = self._parse_status(await self._execute("status"))
        address = next((ip for entry, ip in entries if entry.id == user_id), None)
        if address is None:
            raise CommandRejectedError(
                "The server did not report this player's IP address, so it cannot ban it."
            )
        await self._execute(f"addip 0 {address}")
        await self._execute(self._kick_command(user_id, safe_message(reason)))
        await self._execute("writeip")


def _user_id(player: Player) -> str:
    if not USER_ID.fullmatch(player.id):
        raise CommandRejectedError("This player cannot be named in a command.")
    return player.id
