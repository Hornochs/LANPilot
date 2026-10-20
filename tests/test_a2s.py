"""The own A2S_INFO client: challenge, single-packet answers only, one deadline."""

import asyncio
import bz2
import socket
import struct
import time
from collections.abc import Callable

import pytest

from lanpilot.core.adapters import ProtocolError, ServerUnreachableError
from lanpilot.core.transports import a2s

HEADER = b"\xff\xff\xff\xff"


def text(value: str) -> bytes:
    return value.encode() + b"\x00"


SOURCE_ANSWER = (
    HEADER
    + b"I\x11"
    + text("LAN Server")
    + text("de_anubis")
    + text("csgo")
    + text("Counter-Strike 2")
    + struct.pack("<H", 730)
    + bytes([3, 16, 1])
    + b"dl"
    + bytes([1, 0])
    + text("1.41.9.0")
    + bytes([0x80])
    + struct.pack("<H", 27015)
)
GOLDSRC_ANSWER = (
    HEADER
    + b"m"
    + text("10.0.0.5:27015")
    + text("Old School")
    + text("de_dust2")
    + text("cstrike")
    + text("Counter-Strike")
    + bytes([5, 20, 47])
    + b"dl"
    + bytes([0, 1])  # no password; a mod follows
    + text("http://example.invalid")
    + text("")
    + b"\x00"
    + struct.pack("<II", 1, 1000)
    + bytes([0, 0])
    + bytes([1, 2])  # VAC, bots
)


class _Server(asyncio.DatagramProtocol):
    def __init__(self, answer: Callable[[bytes], list[bytes]]) -> None:
        self.answer = answer
        self.received: list[bytes] = []

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.append(data)
        for packet in self.answer(data):
            self.transport.sendto(packet, addr)


async def serve(
    answer: Callable[[bytes], list[bytes]],
) -> tuple[asyncio.DatagramTransport, _Server, int]:
    loop = asyncio.get_running_loop()
    transport, server = await loop.create_datagram_endpoint(
        lambda: _Server(answer), local_addr=("127.0.0.1", 0), family=socket.AF_INET
    )
    return transport, server, transport.get_extra_info("sockname")[1]


async def test_challenge_is_answered_and_source_layout_parsed() -> None:
    def answer(data: bytes) -> list[bytes]:
        if data == a2s.QUERY:
            return [HEADER + b"A" + b"\x01\x02\x03\x04"]
        assert data == a2s.QUERY + b"\x01\x02\x03\x04"
        return [SOURCE_ANSWER]

    transport, server, port = await serve(answer)
    try:
        info = await a2s.query_info("127.0.0.1", port, 1.0)
    finally:
        transport.close()
    assert len(server.received) == 2
    assert info == a2s.ServerInfo(
        name="LAN Server",
        map="de_anubis",
        folder="csgo",
        game="Counter-Strike 2",
        app_id=730,
        players=3,
        max_players=16,
        bots=1,
        password=True,
        version="1.41.9.0",
        protocol=17,
        port=27015,
    )


def test_old_goldsrc_layout() -> None:
    info = a2s.parse_info(GOLDSRC_ANSWER)
    assert (info.name, info.map, info.folder, info.app_id) == (
        "Old School",
        "de_dust2",
        "cstrike",
        None,
    )
    assert (info.players, info.max_players, info.bots, info.password) == (5, 20, 2, False)


async def test_split_and_compressed_answers_are_refused_without_inflating() -> None:
    bomb = bz2.compress(b"\x00" * 50_000_000)  # a few kilobytes that inflate to 50 MB
    split = (
        b"\xfe\xff\xff\xff"
        + struct.pack("<IBBH", 0x80000001, 1, 0, 1248)
        + struct.pack("<II", 50_000_000, 0)
        + bomb[:1200]
    )
    transport, _, port = await serve(lambda data: [split])
    try:
        started = time.monotonic()
        with pytest.raises(ProtocolError, match="split"):
            await a2s.query_info("127.0.0.1", port, 1.0)
        assert time.monotonic() - started < 0.5
    finally:
        transport.close()


async def test_a_flooding_server_cannot_extend_the_exchange() -> None:
    transport, _, port = await serve(lambda data: [HEADER + b"A\x00\x00\x00\x00"] * 50)
    try:
        with pytest.raises(ProtocolError):
            await a2s.query_info("127.0.0.1", port, 1.0)
    finally:
        transport.close()


async def test_silence_ends_at_the_deadline() -> None:
    transport, _, port = await serve(lambda data: [])
    try:
        started = time.monotonic()
        with pytest.raises(ServerUnreachableError):
            await a2s.query_info("127.0.0.1", port, 0.3)
        assert time.monotonic() - started < 1.0
    finally:
        transport.close()


@pytest.mark.parametrize(
    "packet",
    [
        SOURCE_ANSWER[:20],  # truncated inside a string
        SOURCE_ANSWER[:-20],  # truncated in the numbers
        HEADER + b"X" + SOURCE_ANSWER[5:],  # unknown answer type
        b"garbage",
    ],
)
def test_malformed_answers_raise_protocol_errors(packet: bytes) -> None:
    with pytest.raises(ProtocolError):
        a2s.parse_info(packet)
