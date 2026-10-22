"""discover() of every game adapter, against simulators on 127.0.0.1.

The discovery port constants are pointed at the simulators; the scanned network is the
single host 127.0.0.1, where a "broadcast" is a plain datagram to that address.
"""

from collections.abc import Sequence
from ipaddress import IPv4Network

import pytest

from lanpilot.config import ScanSettings
from lanpilot.core.adapters import DiscoveredServer
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.scan import Scanner
from lanpilot.core.transports.a2s import ServerInfo
from lanpilot.games.counterstrike.goldsrc import CounterStrike16Adapter
from lanpilot.games.counterstrike.source import (
    CounterStrike2Adapter,
    CounterStrikeGlobalOffensiveAdapter,
    CounterStrikeSourceAdapter,
)
from lanpilot.games.trackmania import discovery as tm
from lanpilot.games.trackmania.forever import TrackmaniaForeverAdapter
from lanpilot.games.trackmania.sunrise import TrackmaniaSunriseAdapter
from lanpilot.games.unreal import discovery as unreal
from lanpilot.games.unreal.ut3 import UT3Adapter
from lanpilot.games.unreal.ut99 import UT99Adapter
from lanpilot.games.unreal.ut2004 import UT2004Adapter
from lanpilot.games.valve import common as cs
from lanpilot.testing.goldsrc import GoldSrcSimulator
from lanpilot.testing.lan import (
    TrackmaniaInfoSimulator,
    UT3BeaconSimulator,
    UT99BeaconSimulator,
    UT2004LanSimulator,
)
from lanpilot.testing.source import SourceSimulator
from lanpilot.testing.uweb import UnrealSimulator

LOCAL = [IPv4Network("127.0.0.1/32")]
ELSEWHERE = [IPv4Network("10.255.255.1/32")]
WAIT = 0.2


def free_udp_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --- Counter-Strike ---------------------------------------------------------------------


async def test_counter_strike_servers_are_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    async with (
        GoldSrcSimulator(hostname="Old School") as goldsrc,
        SourceSimulator(game="css", hostname="Source") as css,
        SourceSimulator(game="csgo", hostname="Legacy") as csgo,
        SourceSimulator(game="cs2", hostname="New\x01 One") as cs2,
    ):
        ports = (goldsrc.port, css.port, csgo.port, cs2.port)
        monkeypatch.setattr(cs, "QUERY_PORTS", ports)
        found = {
            adapter.id: await adapter.discover(LOCAL, WAIT)
            for adapter in (
                CounterStrike16Adapter,
                CounterStrikeSourceAdapter,
                CounterStrikeGlobalOffensiveAdapter,
                CounterStrike2Adapter,
            )
        }
    assert found == {
        "cs16": [DiscoveredServer("cs16", "127.0.0.1", goldsrc.port, name="Old School")],
        "css": [DiscoveredServer("css", "127.0.0.1", css.port, name="Source")],
        "csgo": [DiscoveredServer("csgo", "127.0.0.1", csgo.port, name="Legacy")],
        "cs2": [DiscoveredServer("cs2", "127.0.0.1", cs2.port, name="New One")],
    }


async def test_counter_strike_adapters_of_one_scan_share_one_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SourceSimulator(game="cs2") as cs2:
        monkeypatch.setattr(cs, "QUERY_PORTS", (cs2.port,))
        registry = load_registry()
        result = await Scanner(
            registry, ScanSettings(networks=["127.0.0.1/32"], timeout_seconds=0.5)
        ).run()
    # Only the Counter-Strike finds: other games' default ports may answer on this host.
    finds = [(item.server.adapter_id, item.server.port) for item in result.found]
    assert [find for find in finds if find[0] in ("cs16", "css", "csgo", "cs2")] == [
        ("cs2", cs2.port)
    ]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"app_id": 10, "folder": "cstrike"}, "cs16"),
        ({"app_id": None, "folder": "cstrike"}, "cs16"),  # the old GoldSrc layout
        ({"app_id": 10, "folder": "valve"}, None),  # Half-Life
        ({"app_id": 240, "folder": "cstrike"}, "css"),
        ({"app_id": 730, "game": "Counter-Strike 2", "version": "1.41.9.0"}, "cs2"),
        (
            {"app_id": 730, "game": "Counter-Strike: Global Offensive", "version": "1.38.8.1"},
            "csgo",
        ),
        ({"app_id": 730, "game": "", "version": "1.40.0.0"}, "cs2"),
        ({"app_id": 730, "game": "", "version": "odd"}, "csgo"),
        ({"app_id": 440, "folder": "tf"}, "tf2"),
        ({"app_id": 70, "folder": "valve"}, "hldm"),
        ({"app_id": 20, "folder": "valve"}, None),
        ({"app_id": None, "folder": "dod"}, "dod"),
        ({"app_id": 4000, "folder": "garrysmod"}, None),
    ],
)
def test_classify(fields: dict[str, object], expected: str | None) -> None:
    info = ServerInfo(
        **{
            "name": "x",
            "map": "de_dust2",
            "folder": "csgo",
            "game": "",
            "players": 0,
            "max_players": 10,
            "bots": 0,
            "password": False,
            "version": "",
            "protocol": 17,
            **fields,
        }  # type: ignore[arg-type]
    )
    assert cs.classify(info) == expected


# --- Trackmania -------------------------------------------------------------------------


async def test_trackmania_forever_is_found_by_its_session_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with TrackmaniaInfoSimulator(game="forever", name="$f00Red $fffLine") as server:
        monkeypatch.setattr(tm, "SESSION_PORTS", (server.session_port,))
        found = await TrackmaniaForeverAdapter.discover(LOCAL, WAIT)
        # The checksum keys differ: Sunrise's query is not answered by Forever.
        assert await TrackmaniaSunriseAdapter.discover(LOCAL, WAIT) == []
    assert found == [
        DiscoveredServer("tmforever", "127.0.0.1", name="Red Line", game_type="TimeAttack")
    ]


async def test_trackmania_sunrise_is_found_by_its_session_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with (
        TrackmaniaInfoSimulator(game="sunrise", name="Island Fun", game_mode=3) as server,
        TrackmaniaInfoSimulator(game="sunrise", game_id="TmNationsESWC") as other,
    ):
        monkeypatch.setattr(tm, "SESSION_PORTS", (server.session_port, other.session_port))
        found = await TrackmaniaSunriseAdapter.discover(LOCAL, WAIT)
        assert await TrackmaniaForeverAdapter.discover(LOCAL, WAIT) == []
    assert sorted(found, key=lambda item: item.game_type or "") == [
        DiscoveredServer(
            "tmsunrise",
            "127.0.0.1",
            name="Simulated TM",
            game_type="TrackMania Nations ESWC, TimeAttack",
        ),
        DiscoveredServer(
            "tmsunrise", "127.0.0.1", name="Island Fun", game_type="TrackMania Sunrise, Rounds"
        ),
    ]


# --- Unreal Tournament ------------------------------------------------------------------


async def test_ut99_is_found_through_its_beacon(monkeypatch: pytest.MonkeyPatch) -> None:
    server = UnrealSimulator(game="ut99", hostname="Facing Worlds")
    await server.start()
    try:
        async with UT99BeaconSimulator(server.query_port) as beacon:
            monkeypatch.setattr(unreal, "UT99_BEACON_PORTS", (beacon.port,))
            found = await UT99Adapter.discover(LOCAL, WAIT)
            assert await UT99Adapter.discover(ELSEWHERE, WAIT) == []
    finally:
        await server.stop()
    assert found == [
        DiscoveredServer(
            "ut99",
            "127.0.0.1",
            name="Facing Worlds",
            game_type=server.game_class.split(".")[1],
            values={"query_port": str(server.query_port)},
        )
    ]


async def test_ut2004_is_found_and_gets_its_gamespy_port(monkeypatch: pytest.MonkeyPatch) -> None:
    async with UT2004LanSimulator(name="Onslaught", game_type="ONSOnslaughtGame") as server:
        monkeypatch.setattr(unreal, "UT2004_LAN_PORT", server.port)
        found = await UT2004Adapter.discover(LOCAL, WAIT)
    assert found == [
        DiscoveredServer(
            "ut2004",
            "127.0.0.1",
            name="Onslaught",
            game_type="ONSOnslaughtGame",
            values={"query_port": "7787"},
        )
    ]


async def test_ut3_is_found_through_its_beacon(monkeypatch: pytest.MonkeyPatch) -> None:
    answer_port = free_udp_port()
    async with UT3BeaconSimulator(answer_port, name="Coret") as beacon:
        monkeypatch.setattr(unreal, "UT3_BEACON_PORT", beacon.port)
        monkeypatch.setattr(unreal, "UT3_ANSWER_PORT", answer_port)
        found = await UT3Adapter.discover(LOCAL, WAIT)
    assert found == [
        DiscoveredServer("ut3", "127.0.0.1", name="Coret", game_type="Capture The Flag")
    ]


async def test_odd_answers_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async with UT99BeaconSimulator(0) as beacon:  # announces query port 0
        monkeypatch.setattr(unreal, "UT99_BEACON_PORTS", (beacon.port,))
        monkeypatch.setattr(unreal, "UT2004_LAN_PORT", beacon.port)
        monkeypatch.setattr(tm, "SESSION_PORTS", (beacon.port,))
        found: Sequence[DiscoveredServer] = [
            *await UT99Adapter.discover(LOCAL, WAIT),
            *await UT2004Adapter.discover(LOCAL, WAIT),
            *await TrackmaniaSunriseAdapter.discover(LOCAL, WAIT),
        ]
    assert found == []


def test_session_answers_that_do_not_fit_are_refused() -> None:
    def answer(nonce: int, *strings: bytes, tail: bytes = b"\x01\x00\x00\x7f\x2e\x09") -> bytes:
        body = b"".join(len(item).to_bytes(4, "little") + item for item in strings)
        return nonce.to_bytes(4, "little") + body + tail

    good = answer(7, b"GameNet", b"TmForever", b"100", b"host")
    assert tm.parse_session(good, 7, nonce_echoed=True) == tm.Session("TmForever", "host", 2350)
    assert (
        tm.parse_session(
            answer(0, b"GameNet", b"TmForever", b"100", b"host"), 7, nonce_echoed=False
        )
        is not None
    )
    assert tm.parse_session(good, 8, nonce_echoed=True) is None  # another query's answer
    assert tm.parse_session(good[:-3], 7, nonce_echoed=True) is None  # truncated
    overlong = answer(7, b"x" * (tm.MAX_STRING + 1), b"", b"", b"")
    assert tm.parse_session(overlong, 7, nonce_echoed=True) is None
