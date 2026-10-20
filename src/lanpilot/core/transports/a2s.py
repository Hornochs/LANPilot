"""The Source engine server query A2S_INFO (UDP), as Source, Source 2 and GoldSrc servers
answer it.

Used instead of opengsq's ``Source.get_info()``, which inflates bz2-compressed split answers
without a limit: one forged datagram of a few hundred bytes made it allocate gigabytes. An
A2S_INFO answer fits in one datagram for every game LANPilot supports, so this client
accepts exactly that and nothing else:

* ``FF FF FF FF 54 "Source Engine Query" 00`` is sent; a challenge (``FF FF FF FF 41`` + 4
  bytes) is answered by repeating the query with it, at most twice;
* the answer is ``FF FF FF FF 49`` (Source layout, also sent by current GoldSrc servers) or
  ``FF FF FF FF 6D`` (the old GoldSrc layout); split packets (``FE FF FF FF``) are refused;
* the whole exchange has one deadline and reads at most a few datagrams.

Measured answers: docs/games/counter-strike.md.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from dataclasses import dataclass

from lanpilot.core.adapters import ProtocolError, ServerUnreachableError
from lanpilot.i18n import M

HEADER = b"\xff\xff\xff\xff"
SPLIT = b"\xfe\xff\xff\xff"
QUERY = HEADER + b"TSource Engine Query\x00"
CHALLENGE, SOURCE_INFO, GOLDSRC_INFO = 0x41, 0x49, 0x6D
MAX_DATAGRAMS = 4
MAX_DATAGRAM_BYTES = 1400  # what servers put into one packet; larger means split
MAX_CHALLENGES = 2


@dataclass(frozen=True)
class ServerInfo:
    name: str
    map: str
    folder: str
    game: str
    app_id: int | None  # None in the old GoldSrc layout
    players: int
    max_players: int
    bots: int
    password: bool
    version: str
    protocol: int | None
    port: int | None = None  # the game port, if the answer names it


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def take(self, count: int) -> bytes:
        if self._position + count > len(self._data):
            raise ProtocolError(M("The server's status answer is truncated."))
        part = self._data[self._position : self._position + count]
        self._position += count
        return part

    def byte(self) -> int:
        return self.take(1)[0]

    def short(self) -> int:
        return int(struct.unpack("<H", self.take(2))[0])

    def long(self) -> int:
        return int(struct.unpack("<I", self.take(4))[0])

    def string(self) -> str:
        end = self._data.find(b"\x00", self._position)
        if end < 0:
            raise ProtocolError(M("The server's status answer is truncated."))
        text = self._data[self._position : end]
        self._position = end + 1
        return text.decode("utf-8", errors="replace")

    @property
    def remaining(self) -> int:
        return len(self._data) - self._position


def parse_info(packet: bytes) -> ServerInfo:
    """An A2S_INFO answer (one whole datagram, header included)."""
    if not packet.startswith(HEADER) or len(packet) < 6:
        raise ProtocolError(M("The server's status answer could not be read."))
    reader = _Reader(packet[5:])
    if packet[4] == SOURCE_INFO:
        return _source(reader)
    if packet[4] == GOLDSRC_INFO:
        return _goldsrc(reader)
    raise ProtocolError(M("The server's status answer could not be read."))


def _source(reader: _Reader) -> ServerInfo:
    protocol = reader.byte()
    name, current, folder, game = reader.string(), reader.string(), reader.string(), reader.string()
    app_id = reader.short()
    players, max_players, bots = reader.byte(), reader.byte(), reader.byte()
    reader.take(2)  # server type, environment
    password = reader.byte() == 1
    reader.byte()  # VAC
    if app_id == 2400:  # The Ship has three more bytes here
        reader.take(3)
    version = reader.string()
    port = None
    if reader.remaining:
        flags = reader.byte()
        if flags & 0x80:
            port = reader.short()
    return ServerInfo(
        name,
        current,
        folder,
        game,
        app_id,
        players,
        max_players,
        bots,
        password,
        version,
        protocol,
        port,
    )


def _goldsrc(reader: _Reader) -> ServerInfo:
    reader.string()  # address
    name, current, folder, game = reader.string(), reader.string(), reader.string(), reader.string()
    players, max_players, protocol = reader.byte(), reader.byte(), reader.byte()
    reader.take(2)  # server type, environment
    password = reader.byte() == 1
    if reader.byte() == 1:  # a mod: link, download link, a zero byte, version, size, flags
        reader.string()
        reader.string()
        reader.take(1 + 4 + 4 + 2)
    reader.byte()  # VAC
    bots = reader.byte() if reader.remaining else 0
    return ServerInfo(
        name, current, folder, game, None, players, max_players, bots, password, "", protocol
    )


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | OSError] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: object) -> None:
        self.queue.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        self.queue.put_nowait(exc if isinstance(exc, OSError) else OSError(str(exc)))


async def query_info(host: str, port: int, seconds: float) -> ServerInfo:
    """Ask one server; ``seconds`` bounds the whole exchange."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _Datagrams, remote_addr=(host, port), family=socket.AF_INET
        )
    except OSError as exc:
        raise ServerUnreachableError(M("The server's address cannot be reached.")) from exc
    try:
        async with asyncio.timeout(seconds):
            transport.sendto(QUERY)
            challenges = 0
            for _ in range(MAX_DATAGRAMS):
                answer = await protocol.queue.get()
                if isinstance(answer, OSError):
                    raise ServerUnreachableError(M("Nothing answers on the server's port."))
                if answer.startswith(SPLIT) or len(answer) > MAX_DATAGRAM_BYTES:
                    raise ProtocolError(M("The server sent a split status answer."))
                if len(answer) == 9 and answer[:5] == HEADER + bytes([CHALLENGE]):
                    challenges += 1
                    if challenges > MAX_CHALLENGES:
                        break
                    transport.sendto(QUERY + answer[5:9])
                    continue
                return parse_info(answer)
    except TimeoutError as exc:
        raise ServerUnreachableError(M("The server did not answer the status query.")) from exc
    finally:
        transport.close()
    raise ProtocolError(M("The server's status answer could not be read."))
