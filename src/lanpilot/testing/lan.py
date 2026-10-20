"""Simulators of the LAN discovery answers of Trackmania and Unreal Tournament servers.

Each answers the way the measured servers did (docs/games/trackmania.md,
docs/games/unreal-tournament.md), on 127.0.0.1 and ports chosen by the system; tests point
the discovery's port constants at them and scan ``127.0.0.1/32``.

* :class:`TrackmaniaInfoSimulator`: the answer to the UDP session query, which announces
  the TCP port, and the server info over TCP on that port, as TrackMania Forever
  (``game="forever"``, its own checksum key, no nonce in the answer) and Sunrise
  (``game="sunrise"``) send them to a server browser.
* :class:`UT99BeaconSimulator`: ``REPORTQUERY`` -> ``ut <query port>``.
* :class:`UT2004LanSimulator`: the LAN query on one port, answered from the Unreal 2 query
  port with the server details, which that port also answers.
* :class:`UT3BeaconSimulator`: the Unreal Engine 3 LAN beacon, answering to a fixed port of
  the sender like the real one (always 14001 there).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from collections.abc import Callable
from types import TracebackType
from typing import Any, Literal, Self

from opengsq.protocols.trackmania_nations import TrackmaniaNations
from opengsq.protocols.trackmania_sunrise import TrackmaniaSunrise

LOOPBACK = "127.0.0.1"


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, handle: Callable[[bytes, tuple[str, int]], None]) -> None:
        self.handle = handle

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.handle(data, addr)


class _Simulator:
    """Lifecycle shared by the simulators: UDP endpoints and an optional TCP server."""

    def __init__(self) -> None:
        self._transports: list[asyncio.DatagramTransport] = []
        self._tcp: asyncio.Server | None = None
        #: Queries received, in order.
        self.queries: list[bytes] = []

    async def _udp(self, handle: Callable[[bytes, tuple[str, int]], None]) -> int:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Udp(handle), local_addr=(LOOPBACK, 0), family=socket.AF_INET
        )
        self._transports.append(transport)
        return int(transport.get_extra_info("sockname")[1])

    async def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        if self._tcp is not None:
            self._tcp.close()
            self._tcp = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def _str(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def _address(host: str, port: int) -> bytes:
    return bytes(reversed(socket.inet_aton(host))) + struct.pack("<H", port)


class TrackmaniaInfoSimulator(_Simulator):
    def __init__(
        self,
        *,
        game: Literal["forever", "sunrise"] = "forever",
        name: str = "$f00Simulated $fffTM",
        game_mode: int = 1,  # TimeAttack
        game_id: str | None = None,  # default: TmForever or TmSunrise
    ) -> None:
        super().__init__()
        self.game = game
        self.name = name
        self.game_mode = game_mode
        self.game_id = game_id or ("TmForever" if game == "forever" else "TmSunrise")
        self.port = 0  # TCP: the server info
        self.session_port = 0  # UDP: the session query

    async def start(self) -> None:
        self._tcp = await asyncio.start_server(self._serve, LOOPBACK, 0)
        self.port = self._tcp.sockets[0].getsockname()[1]
        self.session_port = await self._udp(self._session)

    @property
    def _protocol(self) -> Any:  # opengsq has no type hints
        return TrackmaniaNations if self.game == "forever" else TrackmaniaSunrise

    def info(self) -> bytes:
        forever = self.game == "forever"
        head = bytes([0x2D if forever else 0x07]) + _address(LOOPBACK, self.port)
        head += _str("simserver") + _str("#SRV#")
        body = _str("") + bytes([2, 32, 0, 8, 0]) + _str(self.name)
        if forever:
            body += _str("stadium")
        body += struct.pack("<I", 0) + _str("")  # no players, no comment
        tail = bytes([self.game_mode]) + struct.pack("<I", 300000) + bytes([0])
        tail += struct.pack("<I", 0)  # no challenges
        if forever:
            tail += struct.pack("<I", 0)  # no environment ids
        return head + body + tail

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                (length,) = struct.unpack("<I", await reader.readexactly(4))
                message = await reader.readexactly(length)
                self.queries.append(message)
                _, payload = self._protocol.decode_message(message)
                version, subtype = struct.unpack_from("<II", payload)
                if subtype != 7:  # only the info request is answered
                    continue
                (request_id,) = struct.unpack_from("<I", payload, 8)
                data = self.info()
                reply = struct.pack("<IIII", version, 6, request_id, len(data)) + data
                framed = self._protocol.build_message(3, reply)
                if self.game == "sunrise":  # Sunrise's builder leaves out the length
                    framed = struct.pack("<I", len(framed)) + framed
                writer.write(framed)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _session(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queries.append(data)
        try:
            message_type, payload = self._protocol.decode_message(data)
        except Exception:
            return
        (size,) = struct.unpack_from("<I", payload)
        if message_type != 0 or payload[4 : 4 + size].decode() != self.game_id:
            return  # a server answers only for its own game
        position = 4 + size
        (size,) = struct.unpack_from("<I", payload, position)
        (nonce,) = struct.unpack_from("<I", payload, position + 4 + size + 6)
        reply = (
            struct.pack("<I", 0 if self.game == "forever" else nonce)
            + _str("GameNet")
            + _str(self.game_id)
            + _str("100" if self.game == "forever" else "1.043")
            + _str("simhost")
            + _address(LOOPBACK, self.port)
            + _address(LOOPBACK, self.session_port)
        )
        message = self._protocol.build_message(1, reply)
        if self.game == "forever":  # its builder adds the length TCP needs
            message = message[4:]
        self._transports[0].sendto(message, addr)


class UT99BeaconSimulator(_Simulator):
    def __init__(self, query_port: int) -> None:
        super().__init__()
        self.query_port = query_port
        self.port = 0

    async def start(self) -> None:
        self.port = await self._udp(self._answer)

    def _answer(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queries.append(data)
        if data == b"REPORTQUERY":
            self._transports[0].sendto(f"ut {self.query_port}".encode(), addr)


class UT2004LanSimulator(_Simulator):
    def __init__(
        self,
        *,
        name: str = "Simulated UT2004",
        game_type: str = "xDeathMatch",
        game_port: int = 7777,
    ) -> None:
        super().__init__()
        self.name = name
        self.game_type = game_type
        self.game_port = game_port
        self.port = 0  # the LAN query port (10777 on a real server)
        self.query_port = 0  # the Unreal 2 query port, which answers

    async def start(self) -> None:
        self.query_port = await self._udp(self._details_query)
        self.port = await self._udp(self._lan_query)

    def details(self) -> bytes:
        def pascal(text: str) -> bytes:
            data = text.encode() + b"\x00"
            return bytes([len(data)]) + data

        return (
            b"\x80\x00\x00\x00\x00"
            + struct.pack("<I", 0)  # server id
            + b"\x00"  # server address: empty
            + struct.pack("<II", self.game_port, 0)
            + pascal(self.name)
            + pascal("DM-Deck17")
            + pascal(self.game_type)
            + struct.pack("<IIII", 0, 16, 0, 0)
            + pascal("0")
        )

    def _lan_query(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queries.append(data)
        if data == b"\x80\x00\x00\x00\x00":
            self._transports[0].sendto(self.details(), addr)  # from the query port

    def _details_query(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queries.append(data)
        if data == b"\x79\x00\x00\x00\x00":
            self._transports[0].sendto(self.details(), addr)


class UT3BeaconSimulator(_Simulator):
    GAME_ID = 0x4D5707DB

    def __init__(
        self, answer_port: int, *, name: str = "Simulated UT3", game_mode: int = 2
    ) -> None:
        super().__init__()
        self.answer_port = answer_port
        self.name = name
        self.game_mode = game_mode  # 2: Capture The Flag
        self.port = 0  # 14001 on a real server

    async def start(self) -> None:
        self.port = await self._udp(self._answer)

    def reply(self, nonce: bytes) -> bytes:
        def string(text: str) -> bytes:
            data = text.encode()
            return struct.pack("!i", len(data)) + data

        header = struct.pack("!BBI", 5, 1, self.GAME_ID) + b"SR" + nonce
        body = socket.inet_aton(LOOPBACK) + struct.pack("!I", 7777)
        body += struct.pack("!IIII", 30, 0, 32, 0)  # open public, private; public, private
        body += bytes([0, 1, 0, 1, 1, 0, 1, 0, 0])  # flags; second: LAN match
        body += bytes(8) + string(self.name)
        body += struct.pack("!I", 1) + struct.pack("!iiB", 32779, self.game_mode, 1)
        body += struct.pack("!I", 2)
        body += struct.pack("!IB", 1073741825, 4) + string("CTF-Coret") + b"\x01"
        body += struct.pack("!IB", 1073741826, 4) + string("UTGame.UTCTFGame") + b"\x01"
        return header + body

    def _answer(self, data: bytes, addr: tuple[str, int]) -> None:
        self.queries.append(data)
        if len(data) == 16 and data[6:8] == b"SQ":
            # The real beacon answers to its own port number at the sender's address.
            self._transports[0].sendto(self.reply(data[8:16]), (addr[0], self.answer_port))
