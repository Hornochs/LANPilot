"""Finding Trackmania servers on the LAN without a password.

Both generations answer a UDP session query (the one their in-game LAN browser sends) on
their game port, 2350 by default, and only for their own game id, which tells the games
apart. The answer announces the server's game port; the server info over TCP on that port
adds name and game mode. The game port is not the XML-RPC port LANPilot controls a server
through, so a find carries the host only and the form keeps the default XML-RPC port.

The two generations sign their messages with different checksum keys: TrackMania Forever
(game id ``TmForever``) with opengsq's ``TrackmaniaNations`` key, Original, Sunrise and
Nations ESWC with the ``TrackmaniaSunrise`` one. Forever does not echo the query's nonce.
Measured facts: docs/games/trackmania.md.
"""

from __future__ import annotations

import secrets
import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import Any

from opengsq.protocols.trackmania_nations import TrackmaniaNations
from opengsq.protocols.trackmania_sunrise import TrackmaniaSunrise

from lanpilot.core import discovery
from lanpilot.core.adapters import DiscoveredServer
from lanpilot.games.trackmania.common import plain

# A server takes the next free port when 2350 is used by another one.
SESSION_PORTS = (2350, 2351, 2352, 2353)
# Game id -> what the game type column says before the mode; Forever's adapter is one game.
FOREVER_GAMES = {"TmForever": ""}
SUNRISE_GAMES: dict[str, str] = dict(TrackmaniaSunrise.GAMES)
INFO_TIMEOUT_SECONDS = 3.0
CLIENT_NAME = "LANPilot"
QUERY_SESSIONS, ENUM_SESSIONS = 0, 1
MAX_STRING = 256


@dataclass(frozen=True)
class Session:
    game_id: str
    host_name: str
    port: int


def _string(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def session_query(game_id: str, nonce: int) -> bytes:
    """The payload of a session query (CNetFormQuerrySessions); the address is unused."""
    return _string(game_id) + _string(CLIENT_NAME) + bytes(6) + struct.pack("<I", nonce)


def parse_session(payload: bytes, nonce: int, *, nonce_echoed: bool) -> Session | None:
    """A session answer (CNetFormEnumSessions): nonce, application, game id, version, host
    name, then the server's address and port. ``None`` if it does not fit."""
    position = 0

    def take(count: int) -> bytes:
        nonlocal position
        if count < 0 or position + count > len(payload):
            raise ValueError("truncated")
        part = payload[position : position + count]
        position += count
        return part

    def string() -> str:
        (size,) = struct.unpack("<I", take(4))
        if size > MAX_STRING:
            raise ValueError("overlong string")
        return take(size).decode("utf-8", errors="replace")

    try:
        (answered,) = struct.unpack("<I", take(4))
        if nonce_echoed and answered != nonce:
            return None
        _application, game_id, _version, host_name = string(), string(), string(), string()
        take(4)  # the server's address, reversed; the sender's address is used instead
        (port,) = struct.unpack("<H", take(2))
    except (ValueError, struct.error):
        return None
    return Session(game_id, host_name, port)


async def _discover(
    adapter_id: str,
    networks: Sequence[IPv4Network],
    wait: float,
    *,
    protocol: Any,  # opengsq's TrackmaniaNations or TrackmaniaSunrise class
    build: Callable[[bytes], bytes],
    games: dict[str, str],
    nonce_echoed: bool,
    info: Callable[[str, int, str], Awaitable[Any]],
) -> list[DiscoveredServer]:
    nonce = secrets.randbits(32)
    packets = [
        (build(session_query(game_id, nonce)), port) for game_id in games for port in SESSION_PORTS
    ]
    sessions: dict[tuple[str, int], Session] = {}
    for reply in await discovery.broadcast(networks, packets, wait):
        try:
            message_type, payload = protocol.decode_message(reply.data)
        except Exception:  # noqa: S112 - another game's answer or our own query
            continue
        if message_type != ENUM_SESSIONS:
            continue
        session = parse_session(payload, nonce, nonce_echoed=nonce_echoed)
        if session is None or session.game_id not in games:
            continue
        port = session.port if 0 < session.port < 65536 else reply.port
        sessions.setdefault((reply.host, port), session)

    async def detail(host: str, port: int, session: Session) -> DiscoveredServer:
        game = games[session.game_id]
        try:
            details = await info(host, port, session.game_id)
        except Exception:
            return DiscoveredServer(
                adapter_id, host, name=_name(session.host_name), game_type=game or None
            )
        mode = discovery.clean_text(details.game_mode)
        return DiscoveredServer(
            adapter_id,
            host,
            name=_name(details.name) or _name(session.host_name),
            game_type=", ".join(part for part in (game, mode) if part) or None,
        )

    return await discovery.gather_found(
        detail(host, port, session) for (host, port), session in sessions.items()
    )


async def discover_forever(
    adapter_id: str, networks: Sequence[IPv4Network], wait: float
) -> list[DiscoveredServer]:
    async def info(host: str, port: int, game_id: str) -> Any:
        return await TrackmaniaNations(host, port, INFO_TIMEOUT_SECONDS).get_info()

    return await _discover(
        adapter_id,
        networks,
        wait,
        protocol=TrackmaniaNations,
        # Its builder adds the length prefix TCP needs; datagrams carry none.
        build=lambda payload: TrackmaniaNations.build_message(QUERY_SESSIONS, payload)[4:],
        games=FOREVER_GAMES,
        nonce_echoed=False,
        info=info,
    )


async def discover_sunrise(
    adapter_id: str, networks: Sequence[IPv4Network], wait: float
) -> list[DiscoveredServer]:
    async def info(host: str, port: int, game_id: str) -> Any:
        return await TrackmaniaSunrise(host, port, INFO_TIMEOUT_SECONDS).get_info(game_id)

    return await _discover(
        adapter_id,
        networks,
        wait,
        protocol=TrackmaniaSunrise,
        build=lambda payload: TrackmaniaSunrise.build_message(QUERY_SESSIONS, payload),
        games=SUNRISE_GAMES,
        nonce_echoed=True,
        info=info,
    )


def _name(text: str) -> str | None:
    return discovery.clean_text(plain(str(text)))
