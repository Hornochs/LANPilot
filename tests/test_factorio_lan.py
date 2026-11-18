"""Factorio's LAN protocol: announcements and the info reply (docs/games/factorio.md)."""

from __future__ import annotations

import socket
from ipaddress import IPv4Network
from pathlib import Path

import pytest

from lanpilot.core.adapters import DiscoveredServer
from lanpilot.games.factorio import lan
from lanpilot.games.factorio.adapter import FactorioAdapter
from lanpilot.games.factorio.lan import (
    INFO_REQUEST,
    FactorioInfo,
    format_game_type,
    listen_for_announcements,
    parse_announcement,
    parse_info_reply,
    query_info,
)
from lanpilot.testing.lan import FactorioLanSimulator

FIXTURES = Path(__file__).parent / "fixtures" / "factorio"
NO_PLAYERS = (FIXTURES / "info_reply_0_players.bin").read_bytes()
ONE_PLAYER = (FIXTURES / "info_reply_1_player.bin").read_bytes()
NAME_OFFSET = 13  # length byte of the server name in both captures


def with_name(reply: bytes, name: bytes) -> bytes:
    old_length = reply[NAME_OFFSET]
    rest = reply[NAME_OFFSET + 1 + old_length :]
    return reply[:NAME_OFFSET] + bytes([len(name)]) + name + rest


def test_parses_reply_without_players() -> None:
    info = parse_info_reply(NO_PLAYERS)
    assert isinstance(info, FactorioInfo)
    assert info.name == "Factorio LAN Server"
    assert info.description == "A friendly LAN game"
    assert info.version == "2.1.24"
    assert info.build == 87808
    assert info.max_players == 0
    assert info.game_minutes == 144833
    assert info.tags == ("lan",)
    assert info.players == ()
    assert ("example-mod", "1.2.3") in info.mods
    assert ("space-age", "2.1.24") in info.mods
    assert info.space_age is True
    assert info.password_protected is None


def test_parses_reply_with_one_player() -> None:
    info = parse_info_reply(ONE_PLAYER)
    assert info is not None
    assert info.players == ("Engineer",)
    assert info.game_minutes == 144839


def test_reply_without_space_age() -> None:
    reply = NO_PLAYERS.replace(b"\x09space-age", b"\x09space-agf")
    info = parse_info_reply(reply)
    assert info is not None
    assert info.space_age is False


def test_long_strings_use_the_space_optimised_length() -> None:
    long_name = b"N" * 300
    reply = (
        NO_PLAYERS[:NAME_OFFSET]
        + b"\xff"
        + len(long_name).to_bytes(4, "little")
        + long_name
        + NO_PLAYERS[NAME_OFFSET + 1 + NO_PLAYERS[NAME_OFFSET] :]
    )
    info = parse_info_reply(reply)
    assert info is not None
    assert info.name.startswith("NNN")


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"\x12" + NO_PLAYERS[1:],  # another message type
        NO_PLAYERS + b"\x00",  # trailing bytes
        NO_PLAYERS[:NAME_OFFSET] + b"\xc8" + NO_PLAYERS[NAME_OFFSET + 1 :],  # length past the end
        *(NO_PLAYERS[:cut] for cut in range(1, len(NO_PLAYERS))),
    ],
)
def test_rejects_damaged_replies(reply: bytes) -> None:
    assert parse_info_reply(reply) is None


def test_reply_text_is_cleaned() -> None:
    info = parse_info_reply(with_name(NO_PLAYERS, b"A\x00B\x1b[31m"))
    assert info is not None
    assert "\x00" not in info.name
    assert "\x1b" not in info.name
    assert info.name.startswith("A")


@pytest.mark.parametrize(
    ("data", "port"),
    [
        (bytes.fromhex("0f9585"), 34197),
        (bytes.fromhex("2f9585"), 34197),
        (b"\x0f\x95", None),
        (bytes.fromhex("109585"), None),
        (bytes.fromhex("0f958500"), None),
        (bytes.fromhex("0f0000"), None),
    ],
)
def test_parse_announcement(data: bytes, port: int | None) -> None:
    assert parse_announcement(data) == port


def test_format_game_type() -> None:
    assert format_game_type("2.1.24", space_age=True) == "2.1.24 · Space Age"
    assert format_game_type("2.1.24", space_age=False) == "2.1.24"


# --- LAN scan ------------------------------------------------------------------------------

LOCAL = [IPv4Network("127.0.0.1/32")]
ELSEWHERE = [IPv4Network("10.99.0.0/24")]
WAIT = 0.3


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def announce_port(monkeypatch: pytest.MonkeyPatch) -> int:
    port = free_udp_port()
    monkeypatch.setattr(lan, "ANNOUNCE_PORT", port)
    monkeypatch.setattr(lan, "MIN_LISTEN_SECONDS", 0.0)
    return port


async def test_query_info_against_simulator() -> None:
    async with FactorioLanSimulator(reply=ONE_PLAYER) as server:
        info = await query_info("127.0.0.1", server.port)
    assert info is not None
    assert info.name == "Factorio LAN Server"
    assert server.queries == [INFO_REQUEST]


async def test_query_info_by_host_name() -> None:
    async with FactorioLanSimulator(reply=ONE_PLAYER) as server:
        info = await query_info("localhost", server.port, wait=0.5)
    assert info is not None


async def test_query_info_garbage_reply_is_none() -> None:
    async with FactorioLanSimulator(reply=b"\x11junk") as server:
        assert await query_info("127.0.0.1", server.port) is None


async def test_query_info_silent_server_is_none() -> None:
    async with FactorioLanSimulator(reply=None) as server:
        assert await query_info("127.0.0.1", server.port, wait=0.2) is None


async def test_listen_for_announcements(announce_port: int) -> None:
    async with FactorioLanSimulator(reply=ONE_PLAYER, announce_port=announce_port) as server:
        found = await listen_for_announcements(LOCAL, WAIT, port=announce_port)
    assert found == {"127.0.0.1": server.port}


async def test_discover_lists_announced_server(announce_port: int) -> None:
    async with FactorioLanSimulator(reply=ONE_PLAYER, announce_port=announce_port) as server:
        found = await FactorioAdapter.discover(LOCAL, WAIT)
    assert found == [
        DiscoveredServer(
            "factorio",
            "127.0.0.1",
            port=None,
            name="Factorio LAN Server",
            game_type="2.1.24 · Space Age",
            values={"game_port": str(server.port)},
        )
    ]


async def test_discover_keeps_server_with_unparseable_info(announce_port: int) -> None:
    async with FactorioLanSimulator(reply=b"\x11junk", announce_port=announce_port) as server:
        found = await FactorioAdapter.discover(LOCAL, WAIT)
    assert found == [
        DiscoveredServer(
            "factorio",
            "127.0.0.1",
            port=None,
            name=None,
            game_type=None,
            values={"game_port": str(server.port)},
        )
    ]


async def test_discover_ignores_hosts_outside_networks(announce_port: int) -> None:
    async with FactorioLanSimulator(reply=ONE_PLAYER, announce_port=announce_port):
        assert await FactorioAdapter.discover(ELSEWHERE, WAIT) == []


async def test_discover_without_servers(announce_port: int) -> None:
    assert await FactorioAdapter.discover(LOCAL, WAIT) == []
