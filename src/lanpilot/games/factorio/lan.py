"""Factorio's LAN protocol, as measured against Factorio 2.1.24 (docs/games/factorio.md).

It is undocumented; everything here was read from captured packets. The message type is
the first byte's low five bits; bit 0x20 varies between packets and carries no meaning
LANPilot needs.

* **Announcement** (type 0x0F): a server with LAN visibility sends three bytes to the
  broadcast address, UDP port 34196, every two seconds: the type and its game port
  (u16, little-endian). Clients only listen; they never search actively.
* **Info request** (type 0x10): one byte sent to the server's game port. It works unicast,
  so it needs no announcement and crosses routers.
* **Info reply** (type 0x11), in order. Strings and counts are Factorio's space-optimised
  integers: one byte, or 0xFF followed by a u32 (little-endian).

  ===========  ===========================================================
  1 byte       type
  4 bytes      token (unknown meaning)
  8 bytes      unknown, zero in all captures
  string       server name
  3 bytes      version: major, minor, patch
  u32          build
  string       description
  u16          maximum players (0: no limit)
  u32          game time in minutes
  2 bytes      unknown, zero in all captures
  string       host (``0.0.0.0:34197``)
  3 bytes      unknown (``00 00 01``)
  count        mods, each: string name, 3 bytes version, u32 CRC
  count        tags, each a string
  count        online players, each a string
  1 byte       unknown (``01``; a server without password showed "Access: Public")
  ===========  ===========================================================

  Which byte marks a password could not be told from the captures, so
  ``password_protected`` is always ``None``.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network

from lanpilot.core import discovery
from lanpilot.core.discovery import clean_text

INFO_REQUEST = b"\x10"
ANNOUNCE_PORT = 34196
GAME_PORT = 34197

TYPE_MASK = 0x1F
ANNOUNCEMENT = 0x0F
INFO_REPLY = 0x11
MAX_TEXT = 200
MAX_ITEMS = 1024
# Servers announce themselves every two seconds; listen a little longer than that.
MIN_LISTEN_SECONDS = 2.5
MAX_REPLY_BYTES = 64 * 1024


@dataclass(frozen=True)
class FactorioInfo:
    name: str
    description: str
    version: str
    build: int
    max_players: int
    game_minutes: int
    mods: tuple[tuple[str, str], ...]
    tags: tuple[str, ...]
    players: tuple[str, ...]
    password_protected: bool | None

    @property
    def space_age(self) -> bool:
        return any(name == "space-age" for name, _version in self.mods)


def format_game_type(version: str, space_age: bool) -> str:
    return f"{version} · Space Age" if space_age else version


def parse_announcement(data: bytes) -> int | None:
    """The game port a LAN announcement advertises, or ``None`` for anything else."""
    if len(data) != 3 or data[0] & TYPE_MASK != ANNOUNCEMENT:
        return None
    port = int.from_bytes(data[1:3], "little")
    return port or None


class _MalformedError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def take(self, count: int) -> bytes:
        end = self._pos + count
        if end > len(self._data):
            raise _MalformedError
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "little")

    def optimised(self) -> int:
        value = self.uint(1)
        return self.uint(4) if value == 0xFF else value

    def count(self) -> int:
        value = self.optimised()
        if value > MAX_ITEMS:
            raise _MalformedError
        return value

    def text(self) -> str:
        raw = self.take(self.optimised())
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _MalformedError from exc
        return clean_text(decoded, MAX_TEXT) or ""

    def version(self) -> str:
        major, minor, patch = self.take(3)
        return f"{major}.{minor}.{patch}"

    @property
    def done(self) -> bool:
        return self._pos == len(self._data)


def parse_info_reply(data: bytes) -> FactorioInfo | None:
    """Decode an info reply; ``None`` for anything that does not match the layout exactly."""
    reader = _Reader(data)
    try:
        if not data or reader.uint(1) & TYPE_MASK != INFO_REPLY:
            return None
        reader.take(4 + 8)
        name = reader.text()
        version = reader.version()
        build = reader.uint(4)
        description = reader.text()
        max_players = reader.uint(2)
        game_minutes = reader.uint(4)
        reader.take(2)
        reader.text()  # host
        reader.take(3)
        mods = []
        for _ in range(reader.count()):
            mod_name = reader.text()
            mod_version = reader.version()
            reader.take(4)  # CRC
            mods.append((mod_name, mod_version))
        tags = tuple(reader.text() for _ in range(reader.count()))
        players = tuple(reader.text() for _ in range(reader.count()))
        reader.take(1)
        if not reader.done:
            return None
    except _MalformedError:
        return None
    return FactorioInfo(
        name=name,
        description=description,
        version=version,
        build=build,
        max_players=max_players,
        game_minutes=game_minutes,
        mods=tuple(mods),
        tags=tags,
        players=players,
        password_protected=None,
    )


async def listen_for_announcements(
    networks: Sequence[IPv4Network], wait: float, *, port: int | None = None
) -> dict[str, int]:
    """Servers inside ``networks`` that announce themselves, as host -> game port."""
    replies = await discovery.broadcast(
        networks,
        [],
        max(wait, MIN_LISTEN_SECONDS),
        source_port=port or ANNOUNCE_PORT,
        share_port=True,
    )
    found: dict[str, int] = {}
    for reply in replies:
        game_port = parse_announcement(reply.data)
        if game_port is not None:
            found[reply.host] = game_port
    return found


class _Answer(asyncio.DatagramProtocol):
    # The socket is connected to the server, so the kernel only delivers its datagrams.
    def __init__(self) -> None:
        self.answer: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str | int, int]) -> None:
        if not self.answer.done():
            self.answer.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.answer.done():
            self.answer.set_exception(exc)


async def query_info(host: str, port: int, wait: float = 1.0) -> FactorioInfo | None:
    """Ask a server for its LAN info; ``None`` if it does not answer or answers oddly."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _Answer, remote_addr=(host, port), family=socket.AF_INET
        )
    except OSError:
        return None
    try:
        transport.sendto(INFO_REQUEST)
        async with asyncio.timeout(wait):
            data = await protocol.answer
    except (TimeoutError, OSError):
        return None
    finally:
        transport.close()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            protocol.answer.cancel()
    if len(data) > MAX_REPLY_BYTES:
        return None
    return parse_info_reply(data)
